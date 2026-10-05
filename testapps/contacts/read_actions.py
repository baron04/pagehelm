"""Plain-HTML navigation effects, independent of the Vue integration and source.

The test application owns its routes, synchronous render markers and explicit-save
forms. Adversarial widgets and effects are excluded; a read-only HTTP window alone
does not qualify them. This prototype is not installed in the default demo.
"""
from urllib.parse import urljoin, urlsplit

from web_operation_agent.execution.action_admission import (
    ActionEffect, ActionPermit, ActionAdmissionRefused)
from web_operation_agent.state.identity import PrincipalError
from .adapter import APPLICATION_ID


class ContactsReadActions:
    application_id = APPLICATION_ID
    required_scope = "contacts:read"
    actions = frozenset({"goto", "click"})

    def __init__(self, adapter):
        self.adapter = adapter
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
        # These are page routes, not a per-button name list. The adversarial view
        # and workflows with unqualified handlers deliberately have no grant.
        if destination not in {"/contacts", "/contacts/help", "/contacts/new"}:
            raise ActionAdmissionRefused("unreviewed")
        return ActionPermit(intent.binding_digest, ActionEffect.NAVIGATION, self.review, destination)

    def verify(self, permit, intent, receipt, *, context, checkpoint):
        self._actor(context, checkpoint)
        after = receipt.observation
        return self._path(after.url) == permit.postcondition and after.page_route == permit.postcondition
