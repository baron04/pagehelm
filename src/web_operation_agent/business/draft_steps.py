"""One reviewed transient step in an explicit-submit application form.

This optional form port complements scalar FormDraft entry. The application owns
date commit timing and line identities; core binds and consumes the exact command
once. There is no persistent effect, submit, save-on-leave or retry capability.
"""
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable
from urllib.parse import urlsplit

from ..execution.action_admission import (
    ActionAdmissionRefused, ActionEffect, ActionIntent, ActionPermit, ActionResultPending,
    verified_observation)
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


@runtime_checkable
class ApplicationNoEffectPort(Protocol):
    """Optional proof that an acknowledged action left the bound draft unchanged.

    This is deliberately separate from :class:`ApplicationDraftStepPort`: existing
    integrations remain fail closed unless the application implements this proof
    for the bound form/field under the same actor.
    """

    def verify_no_effect(self, permit: DraftStepPermit, intent: ActionIntent,
                         receipt: ActionReceipt, *, observation: PageObservation,
                         context: OperationContext, checkpoint: Callable[[], None]) -> bool: ...


@runtime_checkable
class ApplicationUnverifiedDraftPort(Protocol):
    """Optional proof that an uncertain local draft can be observed further.

    It does not settle the attempted step or allow a replay. The application must
    attest that the guarded step had no persistent business effect.
    """

    def may_continue_unverified(self, permit: DraftStepPermit, intent: ActionIntent,
                                receipt: ActionReceipt, *, observation: PageObservation,
                                context: OperationContext, checkpoint: Callable[[], None]) -> bool: ...


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
        verified = self.port.verify_step(permit, intent, ActionReceipt.from_json(receipt.to_json(include_diagnostics=False)),
            observation=observation, context=context, checkpoint=checkpoint)
        self._check(context, checkpoint)
        if verified is not True:
            raise ActionAdmissionRefused("unverified")

    def verify_no_effect(self, intent, permit, receipt, *, context, checkpoint):
        """Verify a typed, acknowledged no-op without treating it as success.

        Only the explicit ``option_unavailable`` runtime fact is eligible.  The normal
        successful-observation verifier is intentionally not reused because it requires
        ``ok``; this path still checks its binding, actor, origin, freshness and guard.
        """
        self._check(context, checkpoint)
        if (intent.operation_binding not in self._consumed
                or permit.binding_digest != intent.binding_digest
                or self._issued.get(intent.binding_digest) != permit
                or receipt.ok or receipt.error_code != "option_unavailable" or not receipt.no_effect
                or intent.action != "select" or context.binding_digest != intent.operation_binding
                or receipt.action != intent.action or receipt.observation is None
                or not receipt.write_guard_held):
            raise ActionAdmissionRefused("unverified")
        before, after = intent.observation, receipt.observation
        from ..transport.protocol import _remote_args_echo
        echo = dict(receipt.args_echo)
        echoed_epoch = echo.pop("epoch", None)
        if (echo != _remote_args_echo(intent.arguments)
                or ("ref" in intent.arguments and echoed_epoch != before.epoch)
                or receipt.url_before != before.url or receipt.url_after != after.url
                or after.epoch <= before.epoch
                or after.render_state != "ready"
                or urlsplit(after.url)[:2] != urlsplit(before.url)[:2]):
            raise ActionAdmissionRefused("unverified")
        if not isinstance(self.port, ApplicationNoEffectPort) or self.port.verify_no_effect(permit, intent,
                ActionReceipt.from_json(receipt.to_json(include_diagnostics=False)),
                observation=after, context=context, checkpoint=checkpoint) is not True:
            raise ActionAdmissionRefused("unverified")
        self._check(context, checkpoint)

    def may_continue_unverified(self, intent, permit, receipt, *, context, checkpoint, settlement=None):
        """Keep an acknowledged local draft unresolved while the model sees new facts."""
        self._check(context, checkpoint)
        if (intent.operation_binding not in self._consumed
                or permit.binding_digest != intent.binding_digest
                or self._issued.get(intent.binding_digest) != permit
                or not isinstance(self.port, ApplicationUnverifiedDraftPort)):
            return False
        try:
            receipt = ActionReceipt.from_json(receipt.to_json(include_diagnostics=False))
            settlement = (ActionReceipt.from_json(settlement.to_json(include_diagnostics=False))
                          if settlement is not None else None)
            observation = verified_observation(intent, receipt, context=context, settlement=settlement)
        except (ActionAdmissionRefused, ActionResultPending):
            return False
        safe = self.port.may_continue_unverified(permit, intent, receipt,
            observation=observation, context=context, checkpoint=checkpoint)
        self._check(context, checkpoint)
        return safe is True
