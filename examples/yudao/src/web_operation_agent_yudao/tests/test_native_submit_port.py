"""Which interfaces belong to the record the page is working on, from what the page itself read."""
from types import SimpleNamespace as NS
import unittest

from web_operation_agent_yudao.reads.client import YudaoReadRefused
from web_operation_agent_yudao.writes.native_submit import YudaoSubmitPort

DOCS = {"paths": {
    "/admin-api/mes/wm/warehouse/create": {"post": {}},
    "/admin-api/mes/wm/warehouse/update": {"put": {}},
    "/admin-api/mes/wm/warehouse/get": {"get": {}},
    "/admin-api/mes/wm/warehouse/page": {"get": {}},
    "/admin-api/mes/md/unit/delete": {"delete": {}},
}}


class Client:
    def __init__(self, record=None, error=None):
        self.record, self.error, self.calls = record, error, []

    def get(self, path, **params):
        self.calls.append((path, params))
        if self.error:
            raise self.error
        return self.record


def port(client=None):
    return YudaoSubmitPort(client or Client(), NS(), lambda: DOCS)


class PrefixTests(unittest.TestCase):
    def test_the_pages_own_table_wins_over_option_lists_loaded_after_it(self):
        reads = (("/admin-api/mes/wm/warehouse/page", "pageNo=1"), ("/admin-api/system/user/list", ""),
                 ("/admin-api/mes/md/unit/list", ""), ("/admin-api/system/dept/list", ""), ("/admin-api/mes/md/item/list", ""))
        found = port().prefixes(reads, mode="create", record_id="", source="/admin-api/mes/wm/warehouse/page")
        self.assertEqual(found[0], "/admin-api/mes/wm/warehouse/")

    def test_an_edit_follows_the_read_that_loaded_the_record_by_its_id(self):
        reads = (("/admin-api/mes/wm/warehouse/get", "id=5"), ("/admin-api/system/user/get", "id=5x"),
                 ("/admin-api/mes/md/unit/list", ""))
        self.assertEqual(port().prefixes(reads, mode="update", record_id="5", source="/admin-api/mes/wm/warehouse/get"), ("/admin-api/mes/wm/warehouse/",))
        self.assertEqual(port().prefixes((), mode="update", record_id="5", source="/admin-api/mes/wm/warehouse/get"), ())

    def test_row_uses_its_table_source_even_after_another_entity_was_read(self):
        source = "/admin-api/mes/wm/warehouse/page"
        reads = ((source, ""), ("/admin-api/mes/md/unit/page", ""))
        self.assertEqual(port().row_prefixes(reads, {"record_id": "5", "source": source}),
                         ("/admin-api/mes/wm/warehouse/",))
        for row in ({"record_id": "5"}, {"source": "/admin-api/other/page"},
                    {"source": source + "?id=5"}):
            self.assertEqual(port().row_prefixes(reads, row), ())

    def test_form_entity_never_falls_back_to_an_unrelated_recent_read(self):
        reads = (("/admin-api/mes/wm/warehouse/get", "id=5"), ("/admin-api/mes/md/unit/get", "id=5"))
        self.assertEqual(port().prefixes(reads, mode="update", record_id="5"), ())
        self.assertEqual(port().prefixes(reads, mode="update", record_id="5", source="/admin-api/mes/wm/warehouse/get"),
                         ("/admin-api/mes/wm/warehouse/",))

    def test_the_catalogue_is_every_documented_write_under_those_prefixes_only(self):
        found = port().catalog(("/admin-api/mes/wm/warehouse/",))
        self.assertEqual(found, {("POST", "/admin-api/mes/wm/warehouse/create"), ("PUT", "/admin-api/mes/wm/warehouse/update")})

    def test_prefix_inference_without_source_for_unique_get_and_create(self):
        self.assertEqual(
            port().prefixes((("/admin-api/mes/wm/warehouse/get", "id=5"),), mode="update", record_id="5"),
            ("/admin-api/mes/wm/warehouse/",),
        )
        reads = (
            ("/admin-api/mes/wm/warehouse/page", "pageNo=1"),
            ("/admin-api/system/user/list", ""),
        )
        # Only warehouse has documented POST .../create, so create mode infers it uniquely when source=""
        self.assertEqual(
            port().prefixes(reads, mode="create", record_id=""),
            ("/admin-api/mes/wm/warehouse/",),
        )
        # With two creatable list reads, source="" stays ambiguous while explicit source resolves
        docs_two = {"paths": {
            **DOCS["paths"],
            "/admin-api/system/user/create": {"post": {}},
        }}
        p_two = YudaoSubmitPort(Client(), NS(), lambda: docs_two)
        self.assertEqual(p_two.prefixes(reads, mode="create", record_id=""), ())
        self.assertEqual(
            p_two.prefixes(reads, mode="create", record_id="", source="/admin-api/mes/wm/warehouse/page"),
            ("/admin-api/mes/wm/warehouse/",),
        )

    def test_prefix_inference_mode_any_matches_update_only_and_get_without_record_id(self):
        docs_upd = {"paths": {
            "/admin-api/mes/wm/warehouse/update": {"put": {}},
            "/admin-api/mes/wm/warehouse/get": {"get": {}},
            "/admin-api/mes/wm/warehouse/page": {"get": {}},
        }}
        p_upd = YudaoSubmitPort(Client(), NS(), lambda: docs_upd)
        self.assertEqual(
            p_upd.prefixes((("/admin-api/mes/wm/warehouse/page", "pageNo=1"),), mode="any", record_id=""),
            ("/admin-api/mes/wm/warehouse/",),
        )
        self.assertEqual(
            p_upd.prefixes((("/admin-api/mes/wm/warehouse/get", "id=5"),), mode="any", record_id=""),
            ("/admin-api/mes/wm/warehouse/",),
        )


class RecordTests(unittest.TestCase):
    def test_the_record_is_read_as_the_signed_in_person_through_the_prefixs_get(self):
        client = Client({"id": 5, "name": "A"})
        self.assertEqual(port(client).read_record(("/admin-api/mes/wm/warehouse/",), "5"), {"id": 5, "name": "A"})
        self.assertEqual(client.calls, [("mes/wm/warehouse/get", {"id": "5"})])

    def test_a_missing_record_is_none_and_any_other_failure_is_raised_not_guessed(self):
        gone = Client(error=YudaoReadRefused("x: 1 记录不存在", code=1, reason="记录不存在"))
        self.assertIsNone(port(gone).read_record(("/admin-api/a/b/",), "5"))
        down = Client(error=YudaoReadRefused("x: 500 boom", code=500, reason="boom"))
        with self.assertRaises(YudaoReadRefused):
            port(down).read_record(("/admin-api/a/b/",), "5")
        self.assertIsNone(port().read_record(("/admin-api/a/b/",), ""))

    def test_read_record_supplies_required_context_params_from_write_or_reads(self):
        docs = {"paths": {
            "/admin-api/fms/item/get": {"get": {"parameters": [
                {"name": "id", "in": "query", "required": True},
                {"name": "scopeId", "in": "query", "required": True},
            ]}},
        }}
        client = Client({"id": 5, "scopeId": 2, "name": "A"})
        proxy = NS(
            guard=NS(recent_reads=lambda *_a, **_kw: (("/admin-api/fms/item/list", "scopeId=3"),)),
            resolve_actor=lambda *_a: "a" * 64,
        )
        client._login = lambda: "u"
        client.tenant = "1"
        p = YudaoSubmitPort(client, proxy, lambda: docs)
        # Explicit context_params from observed write takes priority
        rec = p.read_record(("/admin-api/fms/item/",), "5", context_params={"scopeId": "2"})
        self.assertEqual(rec["name"], "A")
        self.assertEqual(client.calls[-1], ("fms/item/get", {"id": "5", "scopeId": "2"}))
        # Falls back to recent_reads when context_params is absent
        p.read_record(("/admin-api/fms/item/",), "5")
        self.assertEqual(client.calls[-1], ("fms/item/get", {"id": "5", "scopeId": "3"}))

    def test_read_record_falls_back_to_list_when_get_is_absent(self):
        docs = {"paths": {
            "/admin-api/cfg/item/create": {"post": {}},
            "/admin-api/cfg/item/list": {"get": {}},
        }}
        client = Client([{"id": 1, "name": "first"}, {"id": 2, "name": "second"}])
        p = YudaoSubmitPort(client, NS(), lambda: docs)
        rec = p.read_record(("/admin-api/cfg/item/",), "2")
        self.assertEqual(rec, {"id": 2, "name": "second"})
        self.assertEqual(client.calls, [("cfg/item/list", {})])
        self.assertIsNone(p.read_record(("/admin-api/cfg/item/",), "999"))

    def test_destructive_labels_and_unlabelled_controls_count_as_dangerous(self):
        element = lambda name, **attrs: NS(name=name, attrs=attrs)
        for name in ("删除", "作废", "关闭订单", "重置密码"):
            self.assertTrue(port().dangerous(element(name)))
        self.assertTrue(port().dangerous(element("")))
        for name in ("停用", "审核", "上架"):
            self.assertFalse(port().dangerous(element(name)))


if __name__ == "__main__":
    unittest.main()
