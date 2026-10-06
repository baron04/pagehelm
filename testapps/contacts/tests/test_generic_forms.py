"""The REST/list sample implements the same generic current-form contract as Odoo."""
from __future__ import annotations

import re
import time
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from testapps.contacts.adapter import (
    APPLICATION_ID, MinimalContactsAdapter, _ContactsFormPort,
)
from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.tests.support.form_contract import (
    assert_current_form_contract,
)
from web_operation_agent.business.forms import ApplicationFormField, ApplicationFormTools


class _NativeDatetimeContactsFormPort(_ContactsFormPort):
    """Test-only application contract; the Contacts demo still exposes its usual fields."""

    def describe_form(self, observation, *, context=None):
        form = super().describe_form(observation, context=context)
        if form is None:
            return None
        return replace(form, reference="contacts-native-datetime-fixture-v1", fields=(
            ApplicationFormField("follow_up_time", "Follow-up time", kind="datetime",
                                 binding="label"),))

    def prepare_form(self, form, values, *, context=None):
        normalized = []
        for item in values:
            # This fixture's application accepts a naive ISO local time, with no
            # precision beyond seconds. The portable datetime kind is deliberately
            # broader; the application owns these constraints and native serialization.
            raw = item.value.strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?", raw):
                raise BusinessToolRequestError(
                    "Follow-up time requires a naive ISO local time at second precision")
            try:
                value = datetime.fromisoformat(raw)
            except ValueError:
                raise BusinessToolRequestError("Follow-up time is invalid") from None
            # Native datetime-local omits zero seconds and normalizes the separator
            # to T. Declaring that exact value preserves the Runtime's equality check.
            normalized.append(replace(item, value=value.isoformat(
                timespec="seconds" if value.second else "minutes")))
        return super().prepare_form(form, tuple(normalized), context=context)


def _datetime_observation():
    return PageObservation(1, 7, "http://127.0.0.1:1/contacts/new", "New contact", [], [
        ElementRef("e1", "textbox", "Follow-up time", "input", 7,
                   {"type": "datetime-local", "field_label": "Follow-up time",
                    "form_scope": "form:contact-create"}),
        ElementRef("e2", "button", "Save contact", "button", 7,
                   {"form_scope": "form:contact-create"})], "New contact")


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
    def test_native_datetime_fixture_uses_the_shared_form_contract_without_source(self):
        adapter = MinimalContactsAdapter("http://127.0.0.1:1", api_token="delegated")
        port = _NativeDatetimeContactsFormPort(adapter.ports.forms._executor)
        observation = _datetime_observation()
        for value, expected in (
            ("2026-10-06T09:05:00", "2026-10-06T09:05"),
            ("2026-10-06 09:05:07", "2026-10-06T09:05:07"),
            ("2028-02-29T00:00:01", "2028-02-29T00:00:01"),
            ("2026-10-06T09:05", "2026-10-06T09:05"),
        ):
            with self.subTest(value=value):
                assert_current_form_contract(
                    self, port, application_id=APPLICATION_ID,
                    observation=observation, context=_context(),
                    field_label="Follow-up time", native_field="follow_up_time",
                    value=value, expected_value=expected)
        # The fixture adds no field to the normal demonstration adapter.
        described = ApplicationFormTools(
            adapter.ports.forms, application_id=APPLICATION_ID).describe(
                observation, {"field_query": "Follow-up time"}, context=_context())
        self.assertEqual(described["fields"], [])

    def test_native_datetime_application_refuses_timezone_and_precision_loss(self):
        adapter = MinimalContactsAdapter("http://127.0.0.1:1", api_token="delegated")
        port = _NativeDatetimeContactsFormPort(adapter.ports.forms._executor)
        tools = ApplicationFormTools(port, application_id=APPLICATION_ID)
        observation, context = _datetime_observation(), _context()
        described = tools.describe(observation, {}, context=context)
        field = described["fields"][0]
        self.assertEqual((field["type"], field["control"]), ("datetime", "textbox"))
        # The neutral parser accepts these ISO forms. Only this application's
        # second-precision, timezone-free contract refuses them before a draft exists.
        for value in (
            "2026-10-06", "20261006T090500",
            "2026-10-06T09:05:00Z", "2026-10-06T09:05:00+08:00",
            "2026-10-06T09:05:00.001", "2026-10-06T09:05:00.0000001",
        ):
            with self.subTest(value=value), patch.object(
                    port, "prepare_form", wraps=port.prepare_form) as prepare:
                with self.assertRaises(BusinessToolRequestError):
                    tools.prepare(observation, {
                        "form_id": described["form_id"],
                        "values": [{"field_id": field["field_id"], "value": value}],
                    }, context=context)
                prepare.assert_called_once()

    def test_native_datetime_invalid_calendar_or_time_never_reaches_the_application(self):
        adapter = MinimalContactsAdapter("http://127.0.0.1:1", api_token="delegated")
        port = _NativeDatetimeContactsFormPort(adapter.ports.forms._executor)
        tools = ApplicationFormTools(port, application_id=APPLICATION_ID)
        observation, context = _datetime_observation(), _context()
        described = tools.describe(observation, {}, context=context)
        for value in ("not a time", "2026-02-29T09:05:00", "2026-10-06T24:00:00"):
            with self.subTest(value=value), patch.object(
                    port, "prepare_form", wraps=port.prepare_form) as prepare:
                with self.assertRaises(BusinessToolRequestError):
                    tools.prepare(observation, {
                        "form_id": described["form_id"],
                        "values": [{"field_id": described["fields"][0]["field_id"],
                                    "value": value}],
                    }, context=context)
                prepare.assert_not_called()

    def test_source_free_rest_form_binds_a_choice_id_to_its_visible_label(self):
        adapter = MinimalContactsAdapter("http://127.0.0.1:1", api_token="delegated")
        obs = PageObservation(1, 7, "http://127.0.0.1:1/contacts/new", "New contact", [], [
            ElementRef("e1", "combobox", "Priority", "input", 7,
                       {"field_label": "Priority", "form_scope": "form:contact-create"}),
            ElementRef("e2", "button", "Save contact", "button", 7,
                       {"form_scope": "form:contact-create"})], "New contact")
        tools = ApplicationFormTools(adapter.ports.forms, application_id=APPLICATION_ID)
        context = _context()
        result = tools.describe(obs, {}, context=context)
        draft, _ = tools.prepare(obs, {"form_id": result["form_id"], "values": [
            {"field_id": result["fields"][0]["field_id"], "value": "urgent"}]}, context=context)
        self.assertEqual((draft.fields[0].value, draft.fields[0].option_label, draft.fields[0].control),
                         ("urgent", "Urgent", "combobox"))

    def test_shared_application_neutral_contract(self):
        adapter = MinimalContactsAdapter(
            "http://127.0.0.1:1", api_token="delegated")
        observation = PageObservation(
            schema_version=1, epoch=7,
            url="http://127.0.0.1:1/contacts/new", title="New contact",
            landmarks=[], content="New contact form", elements=[
                ElementRef("e1", "textbox", "Name", "input", 7,
                           {"field": "name", "form_scope": "form:contact-create"}),
                ElementRef("e2", "textbox", "Email", "input", 7,
                           {"field": "email", "form_scope": "form:contact-create"}),
            ])
        assert_current_form_contract(
            self, adapter.ports.forms, application_id=APPLICATION_ID,
            observation=observation, context=_context(), field_label="Name",
            native_field="name", value="  Ada   Byron  ", expected_value="Ada Byron")


if __name__ == "__main__":
    unittest.main()
