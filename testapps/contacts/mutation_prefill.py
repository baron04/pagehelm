"""Contacts REST create proposal to reviewed, explicit-save HTML form handoff."""
from __future__ import annotations

from urllib.parse import urlsplit

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.forms import ApplicationFormValue
from web_operation_agent.business.prefill_conversion import MutationPrefillOffer
from web_operation_agent.execution.draft_handoff import field_identity, handoff_draft
from web_operation_agent.execution.model_loop_contracts import LoopToolResult

from .adapter import APPLICATION_ID, CREATE_CONTACT


class ContactsMutationPrefill:
    required_scope = "contacts:write"

    def __init__(self, adapter):
        self.forms = adapter.ports.forms

    def offer(self, proposal, *, context, observation):
        if proposal.tool != CREATE_CONTACT:
            return None
        context.require_live(application_id=APPLICATION_ID, required_scope=self.required_scope)
        entries = self.forms.form_entries(observation, context=context)
        if (observation.render_state != "ready"
                or observation.page_route != "/contacts"
                or urlsplit(observation.url).path != "/contacts"
                or len(entries) != 1 or entries[0].reference != "contacts-create-entry"):
            return None
        if (set(proposal.args) != {"name", "email"}
                or any(not isinstance(proposal.args[key], str) for key in ("name", "email"))):
            raise BusinessToolRequestError("contact create proposal has no exact form fields")
        return MutationPrefillOffer(proposal.content_digest, context.binding_digest,
                                    "/contacts/new", (("name", proposal.args["name"]),
                                                      ("email", proposal.args["email"])))

    @staticmethod
    def _refused():
        return LoopToolResult("The selected contact form could not be verified.", kind="page",
                              state="unverified", error_code="unverified")

    def handoff(self, offer, proposal, *, context):
        offer.require(proposal, context.operation)
        context.operation.require_live(application_id=APPLICATION_ID,
                                       required_scope=self.required_scope)
        if offer.target != "/contacts/new" or dict(offer.fields) != proposal.args:
            return self._refused()
        current = context.obs
        if (urlsplit(current.url).path != "/contacts" or current.page_route != "/contacts"
                or current.render_state != "ready"):
            return self._refused()
        entry = self.forms.plan_entry("contacts-create-entry", current,
                                      context=context.operation)
        opened = context.page.execute("click", {"ref": entry.control.ref})
        if opened.state != "completed" or opened.receipt is None:
            self.forms.discard_entry(entry, context=context.operation)
            return self._refused()
        reference = self.forms.accept_entry(entry, current, opened.receipt, context.obs,
                                            context=context.operation)
        form = self.forms.describe_form(context.obs, context=context.operation)
        if (form is None or form.reference != reference or form.route != offer.target
                or form.submit_label != "Save contact"):
            return self._refused()
        fields = {field.reference: field for field in form.fields}
        if not {"name", "email"} <= fields.keys():
            return self._refused()
        values = tuple(ApplicationFormValue(fields[key], value, "textbox")
                       for key, value in offer.fields)
        draft = self.forms.prepare_form(form, values, context=context.operation)
        if (draft.persistence != "transient_until_submit" or draft.route != offer.target
                or {item.field: item.value for item in draft.fields} != dict(offer.fields)):
            return self._refused()
        before, expected = context.obs, []
        for item in draft.fields:
            matches = [element for element in context.obs.elements
                       if element.role == item.control
                       and (element.attrs.get("field") == item.field if item.binding == "field"
                            else element.attrs.get("field_label") == item.label)
                       and element.attrs.get("form_scope") == draft.control_scope]
            if len(matches) != 1:
                return self._refused()
            control = matches[0]
            result = context.page.execute("fill", {"ref": control.ref,
                                                   "value": item.value, "commit": True})
            if result.state != "completed":
                return self._refused()
            expected.append((field_identity(control), item.value))
        return handoff_draft(context, before, expected, draft.submit_label)
