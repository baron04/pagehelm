"""Application-owned conversion of an exact mutation proposal to a form handoff.

An offer is made before approval from the prepared proposal. It is held on the
server only; the approval card still shows the proposal's effect and targets.
Choosing the offer consumes that card without authorising a mutation. The
adapter must then prove a transient form draft using the existing page runtime.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .tools.contracts import BusinessMutationProposal
    from ..execution.model_loop_contracts import LoopToolResult, PageProcedureContext
    from ..state.identity import OperationContext
    from ..schemas import PageObservation


@dataclass(frozen=True, slots=True)
class MutationPrefillOffer:
    """Exact server-side target and requested values of one reviewed form path."""

    proposal_digest: str
    operation_context_digest: str
    target: str
    fields: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.proposal_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.proposal_digest)
                or not isinstance(self.operation_context_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.operation_context_digest)
                or not isinstance(self.target, str) or not 1 <= len(self.target) <= 300
                or any(ord(char) < 32 or ord(char) == 127 for char in self.target)
                or not isinstance(self.fields, tuple) or not 1 <= len(self.fields) <= 32
                or any(not isinstance(pair, tuple) or len(pair) != 2
                       or not isinstance(pair[0], str) or not 1 <= len(pair[0]) <= 100
                       or not isinstance(pair[1], str) or len(pair[1]) > 4000
                       for pair in self.fields)
                or len({key for key, _ in self.fields}) != len(self.fields)):
            raise ValueError("mutation prefill offer must bind an exact target and fields")

    def require(self, proposal: BusinessMutationProposal, context: OperationContext) -> None:
        if (self.proposal_digest != proposal.content_digest
                or self.operation_context_digest != context.binding_digest
                or proposal.operation_context_digest != context.binding_digest):
            raise ValueError("mutation prefill offer does not match the prepared proposal")


@runtime_checkable
class MutationPrefillPort(Protocol):
    """One adapter's reviewed conversion, with no mutation dispatch authority."""

    required_scope: str

    def offer(self, proposal: BusinessMutationProposal, *, context: OperationContext,
              observation: PageObservation) -> MutationPrefillOffer | None: ...

    def handoff(self, offer: MutationPrefillOffer, proposal: BusinessMutationProposal, *,
                context: PageProcedureContext) -> LoopToolResult: ...
