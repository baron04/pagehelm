"""The REST/list sample implements the same generic current-form contract as Odoo."""
from __future__ import annotations

import time
import unittest

from testapps.contacts.adapter import APPLICATION_ID, MinimalContactsAdapter
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.tests.support.form_contract import (
    assert_current_form_contract,
)


def _context(scopes=("contacts:read", "contacts:write")):
    now = int(time.time() * 1_000)
    principal = PrincipalContext(
        schema_version=1, application_id=APPLICATION_ID,
        tenant_id="synthetic", workspace_id="workspace", actor_id="writer",
        delegation_id="delegation", issued_at=now - 1_000,
        expires_at=now + 60_000, scopes=scopes,
    )
    return OperationContext.create(
        principal, session_id="session", turn_id="turn",
        operation_id="generic-form", deadline_ms=now + 30_000, now_ms=now)


class ContactsGenericFormTests(unittest.TestCase):
    def test_shared_application_neutral_contract(self):
        adapter = MinimalContactsAdapter(
            "http://127.0.0.1:1", api_token="delegated")
        observation = PageObservation(
            schema_version=1, epoch=7,
            url="http://127.0.0.1:1/contacts/new", title="New contact",
            landmarks=[], content="New contact form", elements=[
                ElementRef("e1", "textbox", "Name", "input", 7, {"field": "name"}),
                ElementRef("e2", "textbox", "Email", "input", 7, {"field": "email"}),
            ])
        assert_current_form_contract(
            self, adapter.ports.forms, application_id=APPLICATION_ID,
            observation=observation, context=_context(), field_label="Name",
            native_field="name", value="  Ada   Byron  ", expected_value="Ada Byron")


if __name__ == "__main__":
    unittest.main()
