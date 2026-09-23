"""Translate actor-scoped Odoo metadata to the generic current-form contract.

Odoo details stop in this module.  The core sees a route, application-labelled scalar
fields and a transient draft; it never sees a model name, action id or ``fields_get``.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
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

# Browser-qualified standard create fields. Metadata proves that a field exists and the
# actor may create a record; it cannot prove that arbitrary onchange code has no external
# effects. Unknown pages and fields remain discoverable through the page, but cannot be
# promoted to a safe prefill draft by this adapter. This is an eligibility policy, not a
# deployment-wide on/off switch or a Skill-owned permission.
_QUALIFIED_CREATE_FIELDS = MappingProxyType({
    ("customers", "res.partner", "account.res_partner_action_customer"):
        frozenset({"name", "email"}),
    ("products", "product.template", "sale.product_template_action"): frozenset({
        "name", "list_price", "invoice_policy"}),
    ("sales", "sale.order", "sale.action_quotations_with_onboarding"):
        frozenset({"validity_date"}),
})


class OdooFormPort:
    """Describe safe scalar fields on the actor's current create form."""

    required_scopes = frozenset({GENERIC_FORM_SCOPE})

    def __init__(self, client, *, qualified_fields: Mapping[
            tuple[str, str, str], frozenset[str]] | None = None):
        self._client = client
        self._reader = OdooRuntimeDescriptorReader(client)
        self._entities = OdooEntityReadPort(client)
        source = _QUALIFIED_CREATE_FIELDS if qualified_fields is None else qualified_fields
        if (not isinstance(source, Mapping)
                or any(not isinstance(key, tuple) or len(key) != 3
                       or not all(isinstance(part, str) and part for part in key)
                       or not isinstance(fields, frozenset)
                       or not fields or not all(isinstance(name, str) and name for name in fields)
                       for key, fields in source.items())):
            raise ValueError("qualified form fields must be an immutable route/model map")
        self._qualified_fields = MappingProxyType(dict(source))

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
        qualified = self._qualified_fields.get(
            (action.route, model.model, action.xml_id), frozenset())
        if not qualified:
            return None
        entity = EntityDescriptor(
            reference=model.model,
            label=model.label,
            search_text=f"{model.label} {action.name}",
        )
        fields = []
        for field in self._entities.fields(entity, context=context):
            if (field.reference not in qualified or field.readonly
                    or len(field.reference) > 80):
                continue
            if field.kind == "text":
                # Odoo gives some scalar Char widgets (including res.partner.name) an
                # ARIA combobox because they offer scalar suggestions. Relational fields
                # have already been excluded by metadata kind, so accepting this role does
                # not authorize relation/autocomplete selection.
                kind, controls = "text", ("textbox", "combobox")
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
        catalog = self._reader.catalog()
        qualified = frozenset()
        for action in catalog.actions:
            if (action.readable and form.route == f"/odoo/{action.route}/new"
                    and form.reference == (f"action:{action.action_id}:{action.model}:"
                                           f"{catalog.descriptor_digest}")):
                qualified = self._qualified_fields.get(
                    (action.route, action.model, action.xml_id), frozenset())
                break
        if not qualified or any(item.field.reference not in qualified for item in values):
            raise BusinessToolContractError(
                "the current Odoo form fields are not qualified for prefill")

        def browser_value(item: ApplicationFormValue) -> str:
            value = item.value if item.field.kind == "text" else item.value.strip()
            # Odoo 18's SelectionField renders native <option> values with
            # JSON.stringify. The application value ``delivery`` is therefore the live DOM
            # value ``"delivery"``. Keep that encoding inside the Odoo adapter; the core
            # form contract and PageController remain application-neutral.
            return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    if item.field.kind == "choice" else value)

        return FormDraft(
            route=form.route,
            fields=tuple(FormDraftField(
                item.field.reference,
                item.field.label,
                browser_value(item),
                item.control,
            ) for item in values),
        )


__all__ = ["GENERIC_FORM_SCOPE", "OdooFormPort"]
