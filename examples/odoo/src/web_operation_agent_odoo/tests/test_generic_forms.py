"""Odoo metadata translation for the application-neutral current-form contract."""
from __future__ import annotations

import copy
import time
import unittest

from web_operation_agent.business.errors import BusinessToolPermissionError
from web_operation_agent.business.forms import ApplicationFormTools
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.tests.support.form_contract import (
    assert_current_form_contract,
)
from web_operation_agent_odoo.server.descriptors import _catalog_digest
from web_operation_agent_odoo.server.generic_forms import (
    GENERIC_FORM_SCOPE,
    OdooFormPort,
)

from .test_generic_reads import _catalog, _fields


class _Client:
    def __init__(self):
        self.catalog = _catalog()
        self.catalog["models"][0]["operations"] = ["create", "read", "write"]
        self.catalog["actions"][0]["route"] = "inventory/transfers"
        self.catalog["descriptor_digest"] = _catalog_digest(
            self.catalog["models"], self.catalog["actions"],
            self.catalog["omitted_models"], self.catalog["omitted_actions"])
        self.calls = []

    def call(self, model, method, args, kwargs=None):
        self.calls.append((model, method, args, kwargs or {}))
        if (model, method) == ("woa.runtime.descriptor", "catalog"):
            return copy.deepcopy(self.catalog)
        if (model, method) == ("stock.picking", "fields_get"):
            return copy.deepcopy(_fields())
        raise AssertionError((model, method, args, kwargs))


def _context(scopes=(GENERIC_FORM_SCOPE,)):
    now = int(time.time() * 1_000)
    principal = PrincipalContext(
        schema_version=1, application_id="odoo", tenant_id="database",
        workspace_id="company-2", actor_id="user-7", delegation_id="delegation",
        issued_at=now - 1_000, expires_at=now + 60_000, scopes=scopes,
    )
    return OperationContext.create(
        principal, session_id="session", turn_id="turn",
        operation_id="generic-form", deadline_ms=now + 30_000, now_ms=now)


def _observation():
    return PageObservation(
        schema_version=1, epoch=4,
        url="https://odoo.test/odoo/inventory/transfers/new",
        title="New transfer", landmarks=[], content="New transfer form", elements=[
            ElementRef("e1", "textbox", "Reference", "input", 4, {"field": "name"}),
            ElementRef("e2", "textbox", "Scheduled Date", "input", 4,
                       {"field": "scheduled_date"}),
            # Relational controls are deliberately absent from the adapter descriptor.
            ElementRef("e3", "combobox", "Carrier", "input", 4,
                       {"field": "carrier_id"}),
        ])


class OdooGenericFormTests(unittest.TestCase):
    def test_shared_application_neutral_contract(self):
        client = _Client()
        assert_current_form_contract(
            self, OdooFormPort(client), application_id="odoo",
            observation=_observation(), context=_context(), field_label="Reference",
            native_field="name", value="WH/OUT/00042")
        self.assertTrue(any(call[1] == "fields_get" for call in client.calls))

    def test_only_runtime_page_intersection_and_safe_scalar_fields_are_returned(self):
        tools = ApplicationFormTools(
            OdooFormPort(_Client()), application_id="odoo", binding_secret=b"o" * 32)
        result = tools.describe(_observation(), {}, context=_context())
        self.assertEqual(
            {item["label"] for item in result["fields"]},
            {"Reference", "Scheduled Date"})
        self.assertNotIn("Carrier", str(result))
        self.assertNotIn("access_token", str(result))

    def test_missing_form_scope_is_refused_before_metadata_rpc(self):
        client = _Client()
        tools = ApplicationFormTools(
            OdooFormPort(client), application_id="odoo", binding_secret=b"o" * 32)
        with self.assertRaises(BusinessToolPermissionError):
            tools.describe(_observation(), {}, context=_context(("unrelated:read",)))
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
