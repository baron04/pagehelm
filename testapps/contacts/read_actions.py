"""Plain-HTML navigation effects, independent of the Vue integration and source.

The test application owns its routes, synchronous render markers and explicit-save
forms. Adversarial widgets and effects are excluded; a read-only HTTP window alone
does not qualify them. This prototype is not installed in the default demo.
"""
from urllib.parse import urljoin, urlsplit
import hashlib
import json

from web_operation_agent.execution.action_admission import (
    ActionEffect, ActionPermit, ActionAdmissionRefused)
from web_operation_agent.state.identity import PrincipalError
from .adapter import APPLICATION_ID


class ContactsReadActions:
    application_id = APPLICATION_ID
    required_scope = "contacts:read"
    actions = frozenset({"goto", "click", "fill"})

    def __init__(self, adapter, *, filters=False):
        self.adapter = adapter
        self.filters, self._queries = filters, {}
        self.review = "contacts-explicit-save-navigation-v1"

    def _actor(self, context, checkpoint):
        checkpoint()
        context.require_live(application_id=APPLICATION_ID, required_scope=self.required_scope)
        identity = self.adapter.ports.identity
        actual, permissions = identity._whoami(
            {"Authorization": f"Bearer {identity._api_token}"}, "delegated_api")
        principal = context.principal
        if ((actual.tenant_id, actual.workspace_id, actual.actor_id)
                != (principal.tenant_id, principal.workspace_id, principal.actor_id)
                or self.required_scope not in permissions):
            raise PrincipalError("the application navigation actor changed")
        checkpoint()

    def _path(self, url):
        parsed = urlsplit(url)
        if (f"{parsed.scheme}://{parsed.netloc}" != self.adapter.descriptor.origin
                or parsed.query or parsed.fragment):
            raise ActionAdmissionRefused("invalid_target")
        return parsed.path

    def admit(self, intent, *, context, checkpoint):
        self._actor(context, checkpoint)
        before = intent.observation
        if before.page_route != self._path(before.url):
            raise ActionAdmissionRefused("invalid_target")
        if self.filters and before.page_route == "/contacts/filter" and intent.action != "goto":
            return self._query(intent)
        if (intent.action == "click" and before.page_route in {
                "/contacts/workflows/partner", "/contacts/workflows/supplier"}):
            target = next(e for e in before.elements if e.ref == intent.arguments["ref"])
            if target.role == "tab" and target.tag == "button" and target.attrs.get("id") in {
                    "workflow-overview", "workflow-terms"}:
                return ActionPermit(intent.binding_digest, ActionEffect.READ_ONLY,
                                    "contacts-workflow-tabs-v1", target.attrs["id"])
            raise ActionAdmissionRefused("unreviewed")
        if intent.action == "goto":
            destination = self._path(urljoin(before.url, intent.arguments["url"]))
        else:
            element = next(e for e in before.elements if e.ref == intent.arguments["ref"])
            if (element.role != "link" or element.tag != "a"
                    or element.attrs.get("form_scope")
                    or element.attrs.get("target", "") not in {"", "_self"}
                    or not element.attrs.get("href")):
                raise ActionAdmissionRefused("unreviewed")
            destination = self._path(urljoin(before.url, element.attrs["href"]))
        # The application owns these navigation and synchronous tab handlers.
        # The adversarial view and its effectful handlers have no grant.
        if destination not in {"/contacts", "/contacts/help", "/contacts/new",
                "/contacts/workflows/partner", "/contacts/workflows/supplier"} | ({"/contacts/filter"} if self.filters else set()):
            raise ActionAdmissionRefused("unreviewed")
        return ActionPermit(intent.binding_digest, ActionEffect.NAVIGATION, self.review, destination)

    def verify(self, permit, intent, receipt, *, observation, context, checkpoint):
        self._actor(context, checkpoint)
        after = observation
        if permit.review == "contacts-workflow-tabs-v1":
            target = next(e for e in intent.observation.elements if e.ref == intent.arguments["ref"])
            selected = [e for e in after.elements if e.role == "tab" and e.name == target.name
                        and e.attrs.get("id") == permit.postcondition]
            return (after.url == intent.observation.url and after.page_route == intent.observation.page_route
                    and len(selected) == 1 and selected[0].attrs.get("aria-selected") == "true")
        if intent.binding_digest in self._queries:
            value, instance, revision = self._queries.pop(intent.binding_digest)
            fields = [e for e in after.elements if e.attrs.get("query_field") == "name_contains"
                      and e.attrs.get("form_scope") == "form:contact-filter"
                      and e.attrs.get("form_instance") == instance]
            if (after.url != intent.observation.url or after.page_route != "/contacts/filter"
                    or len(fields) != 1 or fields[0].attrs.get("value", "") != value):
                return False
            if intent.action == "fill":
                return after.page_read == intent.observation.page_read
            read = after.page_read
            if read is None or read["revision"] <= revision:
                # The HTTP collection and browser digest can settle asynchronously.
                self._queries[intent.binding_digest] = (value, instance, revision)
                from web_operation_agent.execution.action_admission import ActionResultPending
                raise ActionResultPending()
            digest = hashlib.sha256(json.dumps([value], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            return (read["path"] == "/api/contacts" and read["query_fields"] == {"name_contains": digest}
                    and read["page"] == 1)
        return self._path(after.url) == permit.postcondition and after.page_route == permit.postcondition

    def _query(self, intent):
        before, args = intent.observation, intent.arguments
        target = next(e for e in before.elements if e.ref == args["ref"])
        fields = [e for e in before.elements if e.attrs.get("query_field") == "name_contains"
                  and e.attrs.get("form_scope") == "form:contact-filter"]
        if len(fields) != 1 or before.page_read is None:
            raise ActionAdmissionRefused("unreviewed")
        field = fields[0]
        if (target.attrs.get("form_scope") != "form:contact-filter" or not field.attrs.get("form_instance")
                or target.attrs.get("form_instance") != field.attrs["form_instance"]):
            raise ActionAdmissionRefused("stale_binding")
        if intent.action == "fill" and target == field and args.get("commit", True) is True:
            value = args["value"]
        elif intent.action == "click" and target.tag == "button" and target.name == "Search contacts":
            value = field.attrs.get("value", "")
        else:
            raise ActionAdmissionRefused("unreviewed")
        self._queries[intent.binding_digest] = (value, field.attrs["form_instance"], before.page_read["revision"])
        return ActionPermit(intent.binding_digest, ActionEffect.READ_ONLY, "contacts-name-filter-v1", intent.binding_digest)
