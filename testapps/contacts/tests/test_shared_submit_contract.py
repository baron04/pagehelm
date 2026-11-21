"""The shared native-write contract over the contacts app's own declarations —

different record and line keys, different volatile keys, a different accepted-write
envelope and a different timezone than the other adapter running this same suite.
"""
import unittest

from testapps.contacts.adapter import ContactsSubmitPort
from web_operation_agent.tests.execution.write_contract_suite import (
    Fixture, SharedSubmitContractSuite)

PREFIX = "/api/contacts"
RECORD = {"id": "c-7", "name": "Ada", "email": "ada@x", "modified": "t0",
          "modifiedBy": "system", "lines": [{"lineId": "L1", "qty": 1, "price": 5}]}
READBACK = {**RECORD, "name": "Ada B", "modified": "t1"}
LOADED = {"id": "c-7", "name": "Ada", "email": "ada@x", "modified": "t0",
          "lines": [{"lineId": "L1", "qty": 1, "price": 5}]}
FORM = {**LOADED, "name": "Ada B", "lines": [{"lineId": "L1", "qty": 2, "price": 5}]}


class _Client:
    """The application's read interface over a synthetic record store."""

    def __init__(self):
        self._record = dict(READBACK)

    def get(self, path, **params):
        assert path == PREFIX
        return {"contacts": [dict(self._record)]}


def _fixture():
    journal = []
    port = ContactsSubmitPort(_Client(), journal)
    port.note_read(PREFIX)
    prefixes = port.prefixes(port.recent_reads(), mode="update",
                             record_id="c-7", source=PREFIX)
    form_body = {k: v for k, v in FORM.items() if k != "id"}
    return Fixture(
        conv=port.contract(), port=port, prefixes=prefixes,
        catalog=port.catalog(prefixes),
        record_id="c-7", record=dict(RECORD),
        loaded_model=dict(LOADED), form_model=dict(FORM),
        field="name", line_identity="L1", line_field="qty",
        update_method="PUT", update_path=PREFIX, update_query="",
        update_body={**form_body, "contactId": "c-7"},
        foreign_path="/api/contacts/import",
        create_method="POST", create_path=PREFIX,
        create_body={"name": "New", "email": "new@x"},
        create_response=b'{"contact":{"id":"c-9","name":"New"}}', new_id="c-9",
        readback=READBACK,
        user_write=lambda method, path, record_id="", created_id="": journal.append(
            {"method": method, "path": path,
             "record_id": record_id, "created_id": created_id}),
        restorable=({"id": "c-7", "dept": "销售", "qty": 1},
                    {"id": "c-7", "dept": "未分配", "qty": 1},
                    {"id": "c-7", "dept": "未分配", "qty": 2}),
        restored={"id": "c-7", "dept": "销售", "qty": 2},
        unrestorable=({"lines": [{"lineId": "L1"}]},
                      {"lines": [{"lineId": "L1"}, {"lineId": "L2"}]},
                      {"lines": [{"lineId": "L1"}, {"lineId": "L2"}]}))


class ContactsSharedSubmitContractTests(SharedSubmitContractSuite, unittest.TestCase):
    def make_fixture(self):
        return _fixture()


if __name__ == "__main__":
    unittest.main()
