"""Translate reviewed raw-source scalar facts under a live native form verifier.

The verifier is an application-owned server boundary, never a model/browser payload.
It must attest the current entry, actor, mode, record and dialog and the reviewed
transient persistence of the exact component bytes. Source discovery alone cannot
issue that attestation. The default native host installs no verifier or form port.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Protocol
from urllib.parse import urlsplit

from web_operation_agent.business.forms import (
    ApplicationFormDescriptor, ApplicationFormField, ApplicationFormValue)
from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.runtime import FormDraft, FormDraftField
from web_operation_agent.schemas import PageObservation
from web_operation_agent.state.identity import OperationContext

from .source_forms import SourceFormReader


@dataclass(frozen=True, slots=True)
class NativeFormState:
    """Fresh server attestation returned by the application's entry verifier.

    An entry id changes when a dialog closes/reopens, even on the same URL. The
    source digest attests reviewed transient behavior, not inferred handler absence.
    No values, credentials, selector, script or permission are forwarded to the model.
    """
    actor_digest: str
    session_id: str
    turn_id: str
    entry_id: str
    mode: str
    record_id: str
    route: str
    control_scope: str
    component: str
    source_digest: str
    title: str
    submit_label: str

    def __post_init__(self):
        if any(not isinstance(v, str) for v in asdict(self).values()):
            raise ValueError("native form attestation must contain strings")
        if self.mode not in {"create", "update"}:
            raise ValueError("native form mode is invalid")
        if (self.mode == "create" and self.record_id) or (self.mode == "update" and
                (len(self.record_id) > 20 or not self.record_id.isascii()
                 or not self.record_id.isdecimal() or int(self.record_id) <= 0)):
            raise ValueError("native form mode and record do not agree")
        if (not self.entry_id or len(self.entry_id) > 120
                or not self.session_id or not self.turn_id
                or not re.fullmatch(r"[0-9a-f]{64}", self.actor_digest)
                or not re.fullmatch(r"[0-9a-f]{64}", self.source_digest)):
            raise ValueError("native form attestation is incomplete")
        # Reuse the generic descriptor's route/scope/text validation below before use.

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True,
                              ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class NativeFormVerifier(Protocol):
    def current(self, observation: PageObservation, *, context: OperationContext
                ) -> NativeFormState | None:
        """Revalidate entry identity, authorization and persistence on this fresh page."""
        ...

    def validate(self, state: NativeFormState, *, context: OperationContext) -> bool:
        """Confirm the entry remains current immediately before issuing the draft."""
        ...


class YudaoFormPort:
    """On-demand source translation using the existing application-neutral form port."""
    required_scopes = frozenset({"yudao.read"})

    def __init__(self, source: SourceFormReader, verifier: NativeFormVerifier):
        if not callable(getattr(verifier, "current", None)) or not callable(getattr(verifier, "validate", None)):
            raise TypeError("native forms require an application-owned live verifier")
        self._source, self._verifier = source, verifier
        # Only one descriptor is needed for this session's current form. No site catalog.
        self._bound: tuple[ApplicationFormDescriptor, NativeFormState] | None = None

    @staticmethod
    def _identity(state: NativeFormState, context: OperationContext) -> bool:
        context.require_live(application_id="yudao", required_scope="yudao.read")
        return (state.actor_digest == context.principal.actor_authority_digest
                and state.session_id == context.session_id and state.turn_id == context.turn_id)

    def describe_form(self, observation: PageObservation, *, context: OperationContext | None = None
                      ) -> ApplicationFormDescriptor | None:
        self._bound = None
        if context is None or observation.render_state != "ready":
            return None
        context.require_live(application_id="yudao", required_scope="yudao.read")
        state = self._verifier.current(observation, context=context)
        if state is None:
            return None
        if not isinstance(state, NativeFormState):
            raise TypeError("native form verifier returned an invalid attestation")
        actual = urlsplit(observation.url)
        if not self._identity(state, context) or actual.path + ("?" + actual.query if actual.query else "") != state.route:
            return None
        source = self._source.read(state.component)
        if source is None or source.binding_digest != state.source_digest:
            return None
        fields = []
        for item in source.fields:
            # Conditional/handler-bearing fields need their own qualified translation.
            if item.conditional or item.handlers:
                continue
            try:
                fields.append(ApplicationFormField(item.reference, item.label,
                    kind={"email": "email", "tel": "phone", "url": "url", "date": "date",
                          "integer": "integer", "number": "number"}.get(item.input_type, "text"),
                    controls=(("spinbutton",) if item.input_type in {"number", "integer"}
                              else ("textbox", "combobox") if item.input_type == "date"
                              else ("textbox",)),
                    binding="label"))
            except ValueError:
                # Credential-like semantics never enter the generic draft contract.
                continue
        if not fields or self._verifier.validate(state, context=context) is not True:
            return None
        form = ApplicationFormDescriptor("native-" + state.digest, state.title, state.route,
            tuple(fields), scope=f"{state.mode}; " + (f"record {state.record_id}" if state.record_id else "new record"),
            control_scope=state.control_scope, submit_label=state.submit_label)
        self._bound = form, state
        return form

    def prepare_form(self, form: ApplicationFormDescriptor,
                     values: tuple[ApplicationFormValue, ...], *,
                     context: OperationContext | None = None) -> FormDraft:
        bound, self._bound = self._bound, None
        if (context is None or bound is None or bound[0] != form
                or not self._identity(bound[1], context)
                or self._verifier.validate(bound[1], context=context) is not True):
            raise BusinessToolRequestError("the native form entry is absent or changed")
        source = self._source.read(bound[1].component)
        if source is None or source.binding_digest != bound[1].source_digest:
            raise BusinessToolRequestError("the reviewed form source changed")
        available = {item.reference: item for item in form.fields}
        facts = {item.reference: item for item in source.fields}
        if (not isinstance(values, tuple) or not values
                or any(not isinstance(item, ApplicationFormValue) for item in values)
                or len({item.field.reference for item in values}) != len(values)):
            raise BusinessToolRequestError("native form values are empty or repeated")
        normalized = {}
        for item in values:
            if available.get(item.field.reference) != item.field:
                raise BusinessToolRequestError("value does not name a current native field")
            limit = facts[item.field.reference].max_utf16_units
            try:
                units = len(item.value.encode("utf-16-le")) // 2
            except UnicodeEncodeError:
                raise BusinessToolRequestError("native form values require valid Unicode") from None
            if limit is not None and units > limit:
                raise BusinessToolRequestError(f"{item.field.label} exceeds the native input limit")
            fact = facts[item.field.reference]
            normalized[item.field.reference] = item.value
            if fact.input_type in {"number", "integer"}:
                try:
                    value = Decimal(item.value)
                    if (not value.is_finite() or abs(value) > Decimal(2**53 - 1)
                            or Decimal(str(float(value))) != value
                            or fact.minimum is not None and value < Decimal(fact.minimum)
                            or fact.maximum is not None and value > Decimal(fact.maximum)
                            or fact.precision is not None and value.scaleb(fact.precision) != value.scaleb(fact.precision).to_integral_value()):
                        raise ValueError("native numeric input would clamp or round the value")
                    value = Decimal(0) if value == 0 else value
                    normalized[item.field.reference] = (
                        format(value, f".{fact.precision}f") if fact.precision is not None
                        else format(value.normalize(), "f"))
                except (InvalidOperation, ValueError, OverflowError):
                    raise BusinessToolRequestError(
                        f"{item.field.label} exceeds the native numeric range or precision") from None
        return FormDraft(form.route, tuple(FormDraftField(item.field.reference, item.field.label,
            normalized[item.field.reference], item.control, binding=item.field.binding) for item in values),
            control_scope=form.control_scope, submit_label=form.submit_label)


__all__ = ["NativeFormState", "NativeFormVerifier", "YudaoFormPort"]
