"""The shared preparation/approval/once-dispatch boundary for execution strategies."""
from dataclasses import dataclass

from .approvals import MutationApproval
from .errors import (BusinessMutationBlockedError, BusinessMutationIndeterminate,
                     BusinessMutationRefusedError, BusinessToolError,
                     BusinessToolPermissionError, BusinessToolReferenceError)
from .tools.contracts import BusinessMutationProposal, BusinessToolResult
from .dispatch_evidence import mutation_evidence
from .prefill_conversion import MutationPrefillOffer, MutationPrefillPort
from ..state.identity import OperationContext


@dataclass(frozen=True, slots=True)
class WriteSettlement:
    approval: MutationApproval
    result: BusinessToolResult | None = None


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    """How one proposed operation ended.

    Only `completed` carries a result, and that result is one the application verified.
    Every other state names why nothing more of this proposal will run: `reason` is the
    terminal reason a turn ends with, and `asked` says whether the person was shown the
    proposal. Before that point a refusal is a request error the proposer may correct;
    after it nothing is proposed again on the proposer's own initiative.
    """
    state: str
    proposal: BusinessMutationProposal | None = None
    result: BusinessToolResult | None = None
    reason: str = ""
    error: Exception | None = None
    asked: bool = False
    prefill_offer: MutationPrefillOffer | None = None
    prefill_context: OperationContext | None = None


class BusinessWriteService:
    def __init__(self, runtime, approvals, loaded_ids: set[str]):
        self.runtime, self.approvals, self.loaded_ids = runtime, approvals, loaded_ids

    def prepare(self, name, inputs, *, context, skill_id=""):
        if not self.approvals.mutations_enabled:
            from .errors import BusinessToolRequestError
            raise BusinessToolRequestError("approved business writes are disabled")
        return self.runtime.prepare_mutation(name, inputs, self.loaded_ids,
                                              context=context, skill_id=skill_id)

    def approve(self, proposal, *, step, context, identity, offer_prefill=False):
        return self.approvals.approve_mutation(proposal, step=step,
            tool=proposal.tool, context=context, identity=identity,
            offer_prefill=offer_prefill)

    def dispatch(self, proposal, approval, *, step, context, identity):
        with mutation_evidence(proposal, approval, context) as evidence:
            def claimed():
                self.approvals.mutation_claimed(proposal, step=step, tool=proposal.tool,
                    approval_id=approval.approval_id, identity=identity)
                if evidence:
                    evidence.claimed()
            result = self.runtime.execute_mutation(proposal, approval.approved_digest,
                self.loaded_ids, context=context, on_claimed=claimed)
        self.approvals.mutation_completed()
        return result

    def settle(self, proposal, *, step, context, identity, offer_prefill=False):
        approval = self.approve(proposal, step=step, context=context, identity=identity,
                                offer_prefill=offer_prefill)
        if not approval.approved:
            return WriteSettlement(approval)
        result = self.dispatch(proposal, approval, step=step, context=context, identity=identity)
        return WriteSettlement(approval, result)

    def run(self, name, inputs, *, step, context, skill_id="", expected=None,
            prefill_port: MutationPrefillPort | None = None,
            prefill_observation=None,
            check_proposal=None) -> OperationOutcome:
        """One proposed operation from preparation to its verified result, exactly once.

        Preparation is read-only. With `expected`, the application's projection of what it
        prepared must be that change before the person is asked. One approval is asked
        for, one dispatch follows it, and an uncertain outcome is recorded and returned,
        never dispatched again.
        """
        ledger = self.approvals.ledger
        identity = context.audit_projection() if context is not None else {}
        proposal, asked, prefill_offer = None, False, None
        try:
            proposal = self.prepare(name, inputs, context=context, skill_id=skill_id)
            if expected is not None:
                if proposal.effect_projection is None:
                    raise BusinessToolError("application did not project its prepared effect")
                proposal.effect_projection.require(expected)
                ledger.append("skill_effect_bound", step=step, tool=name,
                              proposal_digest=proposal.content_digest, side_effect_possible=False)
            if check_proposal is not None:
                check_proposal(proposal)
            if prefill_port is not None:
                if not isinstance(prefill_port, MutationPrefillPort):
                    raise BusinessToolError("mutation prefill port is invalid")
                if context is not None and prefill_observation is not None:
                    prefill_offer = prefill_port.offer(
                        proposal, context=context, observation=prefill_observation)
                if prefill_offer is not None:
                    if not isinstance(prefill_offer, MutationPrefillOffer):
                        raise BusinessToolError("mutation prefill offer is invalid")
                    prefill_offer.require(proposal, context)
            asked = True
            settlement = self.settle(proposal, step=step, context=context, identity=identity,
                                     offer_prefill=prefill_offer is not None)
        except BusinessMutationIndeterminate as exc:
            self.approvals.mutation_indeterminate()
            ledger.append("business_mutation_indeterminate", step=step, tool=name,
                          reconciliation=exc.reconciliation, side_effect_possible=True)
            return OperationOutcome("indeterminate", proposal, reason="indeterminate_write",
                                    error=exc, asked=asked)
        except BusinessToolPermissionError as exc:
            return OperationOutcome("permission_denied", proposal, reason="permission_denied",
                                    error=exc, asked=asked)
        except BusinessToolReferenceError as exc:
            return OperationOutcome("reference", proposal, reason="agent_refused",
                                    error=exc, asked=asked)
        except (BusinessToolError, ValueError) as exc:
            reason = ("business_mutation_blocked" if isinstance(exc, BusinessMutationBlockedError)
                      else "business_mutation_refused" if isinstance(exc, BusinessMutationRefusedError)
                      else "agent_refused")
            return OperationOutcome("refused", proposal, reason=reason, error=exc, asked=asked)
        if settlement.approval.prefill_selected:
            if prefill_offer is None:
                raise AssertionError("unoffered prefill decision passed approval validation")
            return OperationOutcome("prefill_selected", proposal, reason="prefill_selected",
                                    asked=True, prefill_offer=prefill_offer,
                                    prefill_context=context)
        if not settlement.approval.approved:
            return OperationOutcome("not_approved", proposal, reason=settlement.approval.reason,
                                    asked=True)
        result = settlement.result
        ledger.append("business_mutation_result", step=step, tool=name,
                      proposal_digest=proposal.content_digest, result_digest=result.content_digest,
                      audit=result.audit, completed=True, side_effect_possible=True,
                      identity=identity)
        return OperationOutcome("completed", proposal, result, asked=True)
