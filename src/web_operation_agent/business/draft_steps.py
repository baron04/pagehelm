"""One reviewed transient step in an explicit-submit application form.

This optional form port complements scalar FormDraft entry. The application owns
date commit timing and line identities; core binds and consumes the exact command
once. There is no persistent effect, submit, save-on-leave or retry capability.
"""
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from ..execution.action_admission import (
    ActionAdmissionRefused, ActionEffect, ActionIntent, ActionPermit, verified_observation)
from ..schemas import ActionReceipt, PageObservation
from ..state.identity import OperationContext


@dataclass(frozen=True, slots=True)
class DraftStepPermit:
    binding_digest: str
    review: str
    postcondition: str
    form: str
    instance: str
    step: str
    values: tuple[tuple[str, str], ...] = ()
    persistence: str = "transient_until_submit"

    def __post_init__(self):
        ActionPermit(self.binding_digest, ActionEffect.TRANSIENT_DRAFT, self.review, self.postcondition)
        if (self.persistence != "transient_until_submit"
                or self.step not in {"enter_value", "open_entry", "open_choice", "add_line"}
                or any(not isinstance(v, str) or not 0 < len(v) <= 200 for v in (self.form, self.instance))
                or not isinstance(self.values, tuple) or len(self.values) > 12
                or any(not isinstance(pair, tuple) or len(pair) != 2
                       or any(not isinstance(v, str) or len(v) > 4000 for v in pair) for pair in self.values)
                or (self.step == "enter_value") != bool(self.values)):
            raise ValueError("draft steps require an exact transient form and prepared values")


@runtime_checkable
class ApplicationDraftStepPort(Protocol):
    application_id: str
    required_scope: str
    actions: frozenset[str]

    def handles(self, intent: ActionIntent) -> bool: ...
    def prepare_step(self, intent: ActionIntent, *, context: OperationContext,
                     checkpoint: Callable[[], None]) -> DraftStepPermit: ...
    def verify_step(self, permit: DraftStepPermit, intent: ActionIntent, receipt: ActionReceipt, *,
                    observation: PageObservation, context: OperationContext,
                    checkpoint: Callable[[], None]) -> bool: ...


class DraftStepAdmission:
    def __init__(self, port: ApplicationDraftStepPort, application_id: str):
        if (not isinstance(port, ApplicationDraftStepPort) or port.application_id != application_id
                or not isinstance(port.actions, frozenset) or not port.actions
                or not port.actions <= {"click", "fill", "select"}):
            raise ValueError("draft step port must belong to this application")
        self.port, self.application_id = port, application_id
        self._issued, self._consumed = {}, set()

    def _check(self, context, checkpoint):
        checkpoint()
        if not isinstance(context, OperationContext):
            raise ActionAdmissionRefused("stale_binding")
        context.require_live(application_id=self.application_id, required_scope=self.port.required_scope)

    def prepare(self, action, arguments, observation, *, context, checkpoint):
        self._check(context, checkpoint)
        if action not in self.port.actions:
            raise ActionAdmissionRefused("unreviewed")
        intent = ActionIntent.create(action, arguments, observation, context)
        permit = self.port.prepare_step(intent, context=context, checkpoint=checkpoint)
        self._check(context, checkpoint)
        if not isinstance(permit, DraftStepPermit) or permit.binding_digest != intent.binding_digest:
            raise ActionAdmissionRefused("stale_binding")
        if action in {"fill", "select"}:
            if permit.step != "enter_value" or intent.arguments["value"] not in {v for _, v in permit.values}:
                raise ActionAdmissionRefused("unsafe_effect")
        elif action != "click" or permit.step == "enter_value":
            raise ActionAdmissionRefused("unsafe_effect")
        self._issued[intent.binding_digest] = permit
        return intent, permit

    def consume(self, intent, permit, observation, *, context, checkpoint):
        self._check(context, checkpoint)
        current = ActionIntent.create(intent.action, intent.arguments, observation, context)
        if (current.binding_digest != intent.binding_digest or permit.binding_digest != intent.binding_digest
                or self._issued.get(intent.binding_digest) != permit):
            raise ActionAdmissionRefused("stale_binding")
        if intent.operation_binding in self._consumed:
            raise ActionAdmissionRefused("already_consumed")
        self._consumed.add(intent.operation_binding)

    def verify(self, intent, permit, receipt, *, context, checkpoint, settlement=None, minimum_epoch=0):
        self._check(context, checkpoint)
        if (intent.operation_binding not in self._consumed or permit.binding_digest != intent.binding_digest
                or self._issued.get(intent.binding_digest) != permit):
            raise ActionAdmissionRefused("unverified")
        observation = verified_observation(intent, receipt, context=context,
                                           settlement=settlement, minimum_epoch=minimum_epoch)
        verified = self.port.verify_step(permit, intent, ActionReceipt.from_json(receipt.to_json()),
            observation=observation, context=context, checkpoint=checkpoint)
        self._check(context, checkpoint)
        if verified is not True:
            raise ActionAdmissionRefused("unverified")
