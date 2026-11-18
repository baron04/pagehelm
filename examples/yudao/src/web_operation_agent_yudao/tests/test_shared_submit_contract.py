"""The shared native-write contract over Yudao's own declared conventions."""
import unittest
from types import SimpleNamespace as NS

from web_operation_agent.tests.execution.write_contract_suite import (
    Fixture, SharedSubmitContractSuite)
from web_operation_agent_yudao.writes.native_submit import YudaoSubmitPort
from web_operation_agent_yudao.host.page_guard import NativePageGuard

RECORD = {"id": 7, "name": "泵", "status": 1, "updatedTime": "2026-01-01 00:00:00",
          "lines": [{"id": 2, "qty": 1, "price": 5}]}
READBACK = {**RECORD, "name": "泵B", "updatedTime": "2026-01-02 00:00:00"}
LOADED = {"id": 7, "name": "泵", "status": 1, "updatedTime": "2026-01-01 00:00:00",
          "lines": [{"id": 2, "qty": 1, "price": 5}]}
FORM = {**LOADED, "name": "泵B", "lines": [{"id": 2, "qty": 2, "price": 5}]}
UPDATE = "/admin-api/erp/product/update"
CREATE = "/admin-api/erp/product/create"


class _Client:
    """The application's read interface over a synthetic record store."""
    tenant = "tenant-one"

    def __init__(self):
        self._record = dict(READBACK)

    def _login(self):
        return "person-login"

    def get(self, path, **params):
        assert path == "erp/product/get" and str(params.get("id")) == "7"
        return dict(self._record)


ACTOR = "f" * 64


def _fixture():
    guard = NativePageGuard(frozenset({"/admin-api/erp/product/get"}))
    client = _Client()
    proxy = NS(guard=guard, resolve_actor=lambda login, tenant: ACTOR)
    api_docs = lambda: {"paths": {
        CREATE: {"post": {}}, UPDATE: {"put": {}},
        "/admin-api/erp/product/delete": {"delete": {}},
        "/admin-api/system/user/update": {"put": {}}}}
    port = YudaoSubmitPort(client, proxy, api_docs)
    guard.note_read(ACTOR, "/admin-api/erp/product/get?id=7")
    prefixes = port.prefixes(guard.recent_reads(ACTOR), mode="update",
                           record_id="7", source="/admin-api/erp/product/get")
    return Fixture(
        conv=port.contract(), port=port, prefixes=prefixes,
        catalog=port.catalog(prefixes),
        record_id="7", record=dict(RECORD),
        loaded_model=dict(LOADED), form_model=dict(FORM),
        field="name", line_identity="2", line_field="qty",
        update_method="PUT", update_path=UPDATE, update_query="id=7",
        update_body={**FORM, "id": 7},
        foreign_path="/admin-api/erp/product/purge",
        create_method="POST", create_path=CREATE,
        create_body={"name": "新品", "status": 0},
        create_response=b'{"code":0,"data":17}', new_id="17",
        readback=READBACK,
        user_write=lambda method, path, record_id="", created_id="":
            guard.note_user_write(ACTOR, method, path, record_id, created_id),
        restorable=({"id": 7, "unit": "台", "qty": 1},
                    {"id": 7, "unit": "件", "qty": 1},
                    {"id": 7, "unit": "件", "qty": 2}),
        restored={"id": 7, "unit": "台", "qty": 2},
        unrestorable=({"lines": [{"id": 1}]},
                      {"lines": [{"id": 1}, {"id": 2}]},
                      {"lines": [{"id": 1}, {"id": 2}]}))


class YudaoSharedSubmitContractTests(SharedSubmitContractSuite, unittest.TestCase):
    def make_fixture(self):
        return _fixture()


if __name__ == "__main__":
    unittest.main()
