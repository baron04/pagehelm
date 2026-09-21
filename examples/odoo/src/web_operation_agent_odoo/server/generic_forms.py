"""Translate actor-scoped Odoo metadata to the generic current-form contract.

Odoo details stop in this module.  The core sees a route, application-labelled scalar
fields and a transient draft; it never sees a model name, action id or ``fields_get``.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from web_operation_agent.business.entities import EntityDescriptor
from web_operation_agent.business.forms import (
    ApplicationFormDescriptor,
    ApplicationFormField,
    ApplicationFormValue,
    FormChoice,
)
from web_operation_agent.business.runtime import BusinessToolContractError
from web_operation_agent.business.tools.contracts import FormDraft, FormDraftField

from .descriptors import OdooRuntimeDescriptorReader
from .generic_reads import OdooEntityReadPort, odoo_model_allowed


GENERIC_FORM_SCOPE = "generic_forms:prepare"


class OdooFormPort:
    """Describe safe scalar fields on the actor's current create form."""

    required_scopes = frozenset({GENERIC_FORM_SCOPE})

    def __init__(self, client):
        self._client = client
        self._reader = OdooRuntimeDescriptorReader(client)
        self._entities = OdooEntityReadPort(client)

    def describe_form(self, observation, *, context=None):
        path = urlsplit(observation.url).path
        catalog = self._reader.catalog()
        models = {item.model: item for item in catalog.models}
        actions = [item for item in catalog.actions
                   if item.readable and path == f"/odoo/{item.route}/new"]
        candidates = []
        for action in actions:
            model = models.get(action.model)
            if (model is not None and not model.transient
                    and "create" in model.operations
                    and odoo_model_allowed(model.model)):
                candidates.append((action, model))
        if not candidates:
            return None
        if len(candidates) != 1:
            raise BusinessToolContractError(
                "Odoo returned more than one writable action for the current form")
        action, model = candidates[0]
        entity = EntityDescriptor(
            reference=model.model,
            label=model.label,
            search_text=f"{model.label} {action.name}",
        )
        fields = []
        for field in self._entities.fields(entity, context=context):
            if field.readonly or field.reference == "id" or len(field.reference) > 80:
                continue
            if field.kind == "text":
                kind, controls = "text", ("textbox",)
            elif field.kind in {"integer", "number", "date", "datetime"}:
                kind, controls = field.kind, ("textbox",)
            elif field.kind == "choice" and field.choices and all(
                    isinstance(choice.value, str) for choice in field.choices):
                # Native selects have deterministic value semantics. Odoo's relational and
                # autocomplete comboboxes need option-list interaction and stay out until
                # that is separately qualified.
                kind, controls = "choice", ("select",)
            else:
                continue
            fields.append(ApplicationFormField(
                reference=field.reference,
                label=field.label,
                kind=kind,
                controls=controls,
                required=field.required,
                choices=(tuple(FormChoice(str(choice.value), choice.label)
                               for choice in field.choices)
                         if kind == "choice" else ()),
            ))
        if not fields:
            return None
        return ApplicationFormDescriptor(
            reference=(f"action:{action.action_id}:{model.model}:"
                       f"{catalog.descriptor_digest}"),
            title=action.name or model.label,
            route=path,
            fields=tuple(fields),
            scope=(f"scalar create fields exposed to the current actor on {action.name}"
                   )[:300],
        )

    def prepare_form(self, form, values: tuple[ApplicationFormValue, ...], *,
                     context=None) -> FormDraft:
        return FormDraft(
            route=form.route,
            fields=tuple(FormDraftField(
                item.field.reference,
                item.field.label,
                item.value.strip() if item.field.kind != "text" else item.value,
                item.control,
            ) for item in values),
        )


__all__ = ["GENERIC_FORM_SCOPE", "OdooFormPort"]
