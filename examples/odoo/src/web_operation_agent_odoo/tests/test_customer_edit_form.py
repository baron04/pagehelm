"""Changing a field of an existing customer, the way the person finishes it themselves.

Owner ruling (P7.5): a form the application saves when the page is left may still be
prepared for the person -- being asked first is what makes it safe. So this path reads the
record under the delegated identity, shows a confirmation naming old and new values, types
only then, and never saves. Odoo commits the open form on leaving the page, and the draft
says so rather than claiming the fields are transient.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_operation_agent.business.runtime import (APPLICATION_SAVES_ON_LEAVE,
                                          BusinessMutationRefusedError,
                                          BusinessToolRequestError)
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent_odoo.adapter import (EDITABLE_CUSTOMER_FIELDS,
                                              PREPARE_CUSTOMER_EDIT_TOOL,
                                              OdooApplicationAdapter)
from web_operation_agent_odoo.tests.test_adapter import BASE, _FakeOdoo, _key_file

CUSTOMERS = [{"id": 20, "name": "P75 Harbor Foods", "email": "harbor@example.test",
              "phone": "+1 503 555 0142", "mobile": "", "city": "Portland",
              "country_id": [1, "United States"], "vat": "", "customer_rank": 1}]


def _adapter(root: Path, *, allow_mutations: bool = True) -> OdooApplicationAdapter:
    adapter = OdooApplicationAdapter(
        base_url=BASE, api_key_path=_key_file(root), allow_mutations=allow_mutations,
        mutation_journal=MemoryMutationJournal() if allow_mutations else None)
    adapter.business_tools._executor = _FakeOdoo(
        {"res.partner": [dict(row) for row in CUSTOMERS]}, allow_mutations=allow_mutations)
    return adapter


class PreparingAnEditToAnExistingCustomer(unittest.TestCase):
    def test_the_draft_names_the_record_the_field_and_both_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _adapter(Path(tmp))
            result = adapter.business_tools._executor.execute(
                PREPARE_CUSTOMER_EDIT_TOOL,
                {"record_id": 20, "phone": "+1 512 555 0199"})
            draft = result.form_draft

            self.assertEqual(draft.route, "/odoo/customers/20")
            self.assertEqual(draft.persistence, APPLICATION_SAVES_ON_LEAVE)
            self.assertTrue(draft.needs_confirmation)
            self.assertEqual([field.field for field in draft.fields], ["phone"])
            self.assertEqual(draft.fields[0].value, "+1 512 555 0199")
            # What the person is asked to agree to says which record, which field, and what
            # it is today -- a confirmation that only named the new value would be asking
            # them to approve a change they cannot see.
            self.assertEqual(
                draft.targets,
                ("客户：P75 Harbor Foods", "电话：+1 503 555 0142 → +1 512 555 0199",
                 "保存：由你保存；离开该页面时 Odoo 会自动保存"))
            self.assertEqual(draft.effect, "修改客户的电话")
            self.assertEqual(result.content["current_values"], {"phone": "+1 503 555 0142"})

    def test_an_empty_field_today_is_shown_as_empty_rather_than_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _adapter(Path(tmp))
            result = adapter.business_tools._executor.execute(
                PREPARE_CUSTOMER_EDIT_TOOL, {"record_id": 20, "email": "new@example.test"})
            self.assertIn("harbor@example.test → new@example.test", result.form_draft.targets[1])

    def test_only_the_fields_this_deployment_declares_can_be_prepared(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _adapter(Path(tmp))
            self.assertEqual(set(EDITABLE_CUSTOMER_FIELDS), {"name", "phone", "email"})
            with self.assertRaisesRegex(BusinessToolRequestError, "can prepare only"):
                adapter.business_tools._executor.execute(
                    PREPARE_CUSTOMER_EDIT_TOOL, {"record_id": 20, "vat": "X"})

    def test_a_record_this_account_cannot_read_is_not_prepared(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _adapter(Path(tmp))
            with self.assertRaisesRegex(BusinessToolRequestError, "this account can read"):
                adapter.business_tools._executor.execute(
                    PREPARE_CUSTOMER_EDIT_TOOL, {"record_id": 999, "phone": "+1 512 555 0199"})

    def test_values_the_record_already_holds_are_refused_rather_than_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _adapter(Path(tmp))
            with self.assertRaises(BusinessMutationRefusedError):
                adapter.business_tools._executor.execute(
                    PREPARE_CUSTOMER_EDIT_TOOL,
                    {"record_id": 20, "phone": "+1 503 555 0142"})

    def test_a_deployment_without_writes_does_not_offer_it_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = _adapter(Path(tmp), allow_mutations=False)
            names = {schema["function"]["name"]
                     for schema in reader.business_tools._executor.tool_schemas()}
            self.assertNotIn(PREPARE_CUSTOMER_EDIT_TOOL, names)
            writer = _adapter(Path(tmp))
            names = {schema["function"]["name"]
                     for schema in writer.business_tools._executor.tool_schemas()}
            self.assertIn(PREPARE_CUSTOMER_EDIT_TOOL, names)

    def test_preparing_an_edit_needs_the_same_scope_a_write_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _adapter(Path(tmp))
            self.assertEqual(
                adapter.business_tools.required_scopes[PREPARE_CUSTOMER_EDIT_TOOL],
                "customers:write")


if __name__ == "__main__":
    unittest.main()
