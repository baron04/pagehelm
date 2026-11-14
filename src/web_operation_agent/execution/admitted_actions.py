"""Bind effect admission to the existing page dispatch, budget and result path."""
from dataclasses import dataclass
import hashlib
import json
from typing import Callable

from ..state.identity import OperationContext
from ..transport.browser import TurnCancelled
from ..schemas import ActionReceipt
from .action_admission import ActionIntent, ActionPermit, ActionResultPending


@dataclass(frozen=True)
class AdmittedAction:
    intent: ActionIntent
    permit: ActionPermit
    context: OperationContext
    checkpoint: Callable[[], None]


@dataclass(frozen=True)
class PendingAction:
    admitted: AdmittedAction
    receipt_json: str

    @property
    def receipt(self):
        return ActionReceipt.from_json(json.loads(self.receipt_json))


def prepare_action(session, turn, name, args):
    def checkpoint():
        if session._cancel_requested():
            raise TurnCancelled("the person stopped this page action")
        if session._turn_budget is not None:
            session._turn_budget.check()

    checkpoint()
    context = session._approvals.context(turn.step, name,
        hold_ms=session._approvals.operation_hold_ms)
    intent, permit = session.action_admission.prepare(name, args, turn.obs,
        context=context, checkpoint=checkpoint)
    session.ledger.append("action_effect_admitted", step=turn.step, tool=name,
        binding_digest=intent.binding_digest, effect=permit.effect.value,
        review_digest=hashlib.sha256(permit.review.encode()).hexdigest(),
        postcondition_digest=hashlib.sha256(permit.postcondition.encode()).hexdigest(),
        dispatched=False, identity=context.audit_projection())
    return AdmittedAction(intent, permit, context, checkpoint)


def consume_action(session, admitted, observation):
    session.action_admission.consume(admitted.intent, admitted.permit, observation,
        context=admitted.context, checkpoint=admitted.checkpoint)
    session.ledger.append("action_effect_consumed", tool=admitted.intent.action,
                          binding_digest=admitted.intent.binding_digest, dispatched=False)


def verify_action(session, turn, admitted, receipt, *, settlement=None):
    try:
        session.action_admission.verify(admitted.intent, admitted.permit, receipt,
            context=admitted.context, checkpoint=admitted.checkpoint, settlement=settlement,
            minimum_epoch=turn.obs.epoch)
    except ActionResultPending:
        session._pending_action = PendingAction(admitted, json.dumps(receipt.to_json(include_diagnostics=False),
            ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        session.ledger.append("action_effect_pending", step=turn.step,
            command_id=receipt.command_id, binding_digest=admitted.intent.binding_digest,
            observation_epoch=(settlement or receipt).observation.epoch,
            repeated=False, submitted=False, persisted=False)
        return False
    session._pending_action = None
    session.ledger.append("action_effect_verified", step=turn.step,
        command_id=receipt.command_id, binding_digest=admitted.intent.binding_digest,
        effect=admitted.permit.effect.value, observation_epoch=(settlement or receipt).observation.epoch,
        submitted=False, persisted=False)
    return True
