"""Passive facts from an application-owned mutation transport, separate from claims."""
from contextlib import contextmanager
from contextvars import ContextVar
import uuid

from ..execution.events import current_events, turn_interval

_CURRENT = ContextVar("mutation_transport_evidence", default=None)


class DispatchEvidence:
    def __init__(self, proposal, approval, context):
        self.receipt = {"operation_id": context.operation_id if context else "op-"+proposal.content_digest,
            "proposal_digest": proposal.content_digest, "approval_id": approval.approval_id,
            "dispatch_id": "dispatch-"+uuid.uuid4().hex, "dispatch_state": "not-dispatched",
            "dispatch_count": 0, "approval_consumed_count": 0,
            "source": "application-mutation-boundary", "effect_status": "not-applicable",
            "components": []}

    def claimed(self):
        self.receipt["approval_consumed_count"] += 1

    def begin(self, component_id):
        components = self.receipt["components"]
        duplicate = any(c["component_id"] == component_id for c in components)
        self.receipt["dispatch_count"] += int(not components or duplicate)
        self.receipt["dispatch_state"] = "dispatched"
        component = {"component_id": component_id, "transport_count": 1, "effect_status": "unknown"}
        components.append(component)
        self.receipt["effect_status"] = "unknown"
        return component

    def finish(self):
        states = {c["effect_status"] for c in self.receipt["components"]}
        self.receipt["effect_status"] = ("applied" if states == {"applied"} else
            "rejected" if states == {"rejected"} else "unknown" if states else "not-applicable")


@contextmanager
def mutation_evidence(proposal, approval, context):
    current = current_events()
    evidence = DispatchEvidence(proposal, approval, context) if current else None
    marker = _CURRENT.set(evidence)
    try:
        yield evidence
    finally:
        _CURRENT.reset(marker)
        if evidence:
            evidence.finish()
            current[0].record_dispatch(current[1], evidence.receipt)


class TransportEvidence:
    def __init__(self, component):
        self.component = component
    def applied(self):
        if self.component is not None:
            self.component["effect_status"] = "applied"
    def rejected(self):
        if self.component is not None:
            self.component["effect_status"] = "rejected"


@contextmanager
def mutation_transport(component_id="primary"):
    """Enter immediately at the reviewed one-shot mutation transport call.

    Only the integration's acknowledgement semantics may mark applied/rejected.
    Exceptions, invalid replies and timeouts retain unknown effect status.
    """
    evidence = _CURRENT.get()
    current = current_events()
    if evidence is None and current:
        current[0].unbound_mutation(current[1])
    with turn_interval("application"):
        yield TransportEvidence(evidence.begin(component_id) if evidence else None)


def verified_after_readback():
    """Mark one uncertain transport effect applied after a complete actor-bound readback.

    This is for an adapter that can prove the approved effect despite a lost
    acknowledgement. It does not resolve the mutation journal or dispatch again.
    """
    evidence = _CURRENT.get()
    if evidence is not None:
        components = evidence.receipt["components"]
        if len(components) != 1 or components[0]["effect_status"] != "unknown":
            raise ValueError("readback recovery requires one uncertain dispatch")
        components[0]["effect_status"] = "applied"
        components[0]["proof"] = "actor_readback"
