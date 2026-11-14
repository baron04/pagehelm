"""Which interfaces belong to the record the page is working on, from what the page itself read."""
from types import SimpleNamespace as NS
import unittest

from web_operation_agent_yudao.client import YudaoReadRefused
from web_operation_agent_yudao.native_submit import YudaoSubmitPort

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
        found = port().prefixes(reads, mode="create", record_id="")
        self.assertEqual(found[0], "/admin-api/mes/wm/warehouse/")

    def test_an_edit_follows_the_read_that_loaded_the_record_by_its_id(self):
        reads = (("/admin-api/mes/wm/warehouse/get", "id=5"), ("/admin-api/system/user/get", "id=5x"),
                 ("/admin-api/mes/md/unit/list", ""))
        self.assertEqual(port().prefixes(reads, mode="update", record_id="5"), ("/admin-api/mes/wm/warehouse/",))
        self.assertEqual(port().prefixes((), mode="update", record_id="5"), ())

    def test_the_catalogue_is_every_documented_write_under_those_prefixes_only(self):
        found = port().catalog(("/admin-api/mes/wm/warehouse/",))
        self.assertEqual(found, {("POST", "/admin-api/mes/wm/warehouse/create"), ("PUT", "/admin-api/mes/wm/warehouse/update")})


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

    def test_destructive_labels_and_unlabelled_controls_count_as_dangerous(self):
        element = lambda name, **attrs: NS(name=name, attrs=attrs)
        for name in ("删除", "作废", "关闭订单", "重置密码"):
            self.assertTrue(port().dangerous(element(name)))
        self.assertTrue(port().dangerous(element("")))
        for name in ("停用", "审核", "上架"):
            self.assertFalse(port().dangerous(element(name)))


if __name__ == "__main__":
    unittest.main()
