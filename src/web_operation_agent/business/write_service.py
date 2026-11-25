"""The shared preparation/approval/once-dispatch boundary for execution strategies."""
from dataclasses import dataclass

from .approvals import MutationApproval
from .errors import BusinessToolRequestError
from .tools.contracts import BusinessMutationProposal, BusinessToolResult
from .prefill_conversion import MutationPrefillOffer, MutationPrefillPort
from .write_pipeline import WritePipeline
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
    def __init__(self, runtime, approvals, loaded_ids: set[str],
                 *, pipeline: WritePipeline | None = None):
        self.runtime, self.approvals, self.loaded_ids = runtime, approvals, loaded_ids
        self.pipeline = pipeline or WritePipeline()

    def prepare(self, name, inputs, *, context, skill_id="", spec_decorator=None):
        if not self.approvals.mutations_enabled:
            raise BusinessToolRequestError("approved business writes are disabled")
        if spec_decorator is not None:
            return self.runtime.prepare_mutation(
                name, inputs, self.loaded_ids,
                context=context, skill_id=skill_id,
                spec_decorator=spec_decorator,
            )
        return self.runtime.prepare_mutation(name, inputs, self.loaded_ids,
                                              context=context, skill_id=skill_id)

    def approve(self, proposal, *, step, context, identity, offer_prefill=False):
        return self.approvals.approve_mutation(proposal, step=step,
            tool=proposal.tool, context=context, identity=identity,
            offer_prefill=offer_prefill)

    def dispatch(self, proposal, approval, *, step, context, identity):
        return self.pipeline.dispatch_with_evidence(
            proposal=proposal,
            approval=approval,
            approvals=self.approvals,
            step=step,
            context=context,
            identity=identity,
            execute_fn=lambda claimed: self.runtime.execute_mutation(
                proposal, approval.approved_digest,
                self.loaded_ids, context=context, on_claimed=claimed,
            ),
        )

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
            check_proposal=None,
            spec_decorator=None) -> OperationOutcome:
        """One proposed operation from preparation to its verified result, exactly once.

        Preparation is read-only. With `expected`, the application's projection of what it
        prepared must be that change before the person is asked. One approval is asked
        for, one dispatch follows it, and an uncertain outcome is recorded and returned,
        never dispatched again.
        """
        identity = context.audit_projection() if context is not None else {}
        outcome = self.pipeline.run_service_write(
            name=name,
            approvals=self.approvals,
            step=step,
            context=context,
            prepare_fn=lambda: self.prepare(
                name, inputs, context=context, skill_id=skill_id,
                **({"spec_decorator": spec_decorator} if spec_decorator is not None else {}),
            ),
            approve_fn=lambda proposal, offer_prefill: self.approve(
                proposal, step=step, context=context, identity=identity, offer_prefill=offer_prefill
            ),
            dispatch_fn=lambda proposal, approval: self.dispatch(
                proposal, approval, step=step, context=context, identity=identity
            ),
            expected=expected,
            prefill_port=prefill_port,
            prefill_observation=prefill_observation,
            check_proposal=check_proposal,
            acquire_single_flight=False,
        )
        return OperationOutcome(
            outcome.state,
            outcome.proposal,
            outcome.result,
            reason=outcome.reason,
            error=outcome.error,
            asked=outcome.asked,
            prefill_offer=outcome.prefill_offer,
            prefill_context=outcome.prefill_context,
        )
