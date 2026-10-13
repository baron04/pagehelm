"""The shared preparation/approval/once-dispatch boundary for execution strategies."""
from dataclasses import dataclass

from .approvals import MutationApproval
from .tools.contracts import BusinessToolResult
from .dispatch_evidence import mutation_evidence


@dataclass(frozen=True, slots=True)
class WriteSettlement:
    approval: MutationApproval
    result: BusinessToolResult | None = None


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
