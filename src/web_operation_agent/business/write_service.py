"""The shared preparation/approval/once-dispatch boundary for execution strategies."""
from dataclasses import dataclass

from .approvals import MutationApproval
from .errors import (BusinessMutationBlockedError, BusinessMutationIndeterminate,
                     BusinessMutationRefusedError, BusinessToolError,
                     BusinessToolPermissionError, BusinessToolReferenceError)
from .tools.contracts import BusinessMutationProposal, BusinessToolResult
from .dispatch_evidence import mutation_evidence


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


class BusinessWriteService:
    def __init__(self, runtime, approvals, loaded_ids: set[str]):
        self.runtime, self.approvals, self.loaded_ids = runtime, approvals, loaded_ids

    def prepare(self, name, inputs, *, context, skill_id=""):
        if not self.approvals.mutations_enabled:
            from .errors import BusinessToolRequestError
            raise BusinessToolRequestError("approved business writes are disabled")
        return self.runtime.prepare_mutation(name, inputs, self.loaded_ids,
                                              context=context, skill_id=skill_id)

    def approve(self, proposal, *, step, context, identity):
        return self.approvals.approve_mutation(proposal, step=step,
            tool=proposal.tool, context=context, identity=identity)

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

    def settle(self, proposal, *, step, context, identity):
        approval = self.approve(proposal, step=step, context=context, identity=identity)
        if not approval.approved:
            return WriteSettlement(approval)
        result = self.dispatch(proposal, approval, step=step, context=context, identity=identity)
        return WriteSettlement(approval, result)

    def run(self, name, inputs, *, step, context, skill_id="", expected=None) -> OperationOutcome:
        """One proposed operation from preparation to its verified result, exactly once.

        Preparation is read-only. With `expected`, the application's projection of what it
        prepared must be that change before the person is asked. One approval is asked
        for, one dispatch follows it, and an uncertain outcome is recorded and returned,
        never dispatched again.
        """
        ledger = self.approvals.ledger
        identity = context.audit_projection() if context is not None else {}
        proposal, asked = None, False
        try:
            proposal = self.prepare(name, inputs, context=context, skill_id=skill_id)
            if expected is not None:
                if proposal.effect_projection is None:
                    raise BusinessToolError("application did not project its prepared effect")
                proposal.effect_projection.require(expected)
                ledger.append("skill_effect_bound", step=step, tool=name,
                              proposal_digest=proposal.content_digest, side_effect_possible=False)
            asked = True
            settlement = self.settle(proposal, step=step, context=context, identity=identity)
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
        if not settlement.approval.approved:
            return OperationOutcome("not_approved", proposal, reason=settlement.approval.reason,
                                    asked=True)
        result = settlement.result
        ledger.append("business_mutation_result", step=step, tool=name,
                      proposal_digest=proposal.content_digest, result_digest=result.content_digest,
                      audit=result.audit, completed=True, side_effect_possible=True,
                      identity=identity)
        return OperationOutcome("completed", proposal, result, asked=True)
