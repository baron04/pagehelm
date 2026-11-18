"""A Yudao rows read owns its people display table for only that call."""
from copy import deepcopy
import unittest

from web_operation_agent.goal.data import EVERYTHING, Compare, DataRefused
from web_operation_agent_yudao.goal_compat.goal import YudaoGoalData
from web_operation_agent_yudao.tests.test_goal import DOCS, _Client, _prop


class _PeopleClient(_Client):
    def __init__(self):
        super().__init__()
        self.denied = set()
        self.people_name = "Current owner"
        self.read_generation = 0
        self.renew_during_people = False
        self.duplicate_people = False
        self.records = [dict(id=i + 1, name=f"Synthetic {i}", totalPrice=20,
                             ownerUserId=257, reviewerUserId=249, ownerDeptId=7)
                        for i in range(64)]

    def get(self, path, **params):
        if path in ("system/user/simple-list", "system/dept/simple-list"):
            self.calls.append((path, params))
            if path in self.denied:
                raise DataRefused("actor was refused", permission_denied=True)
            if self.renew_during_people:
                self.read_generation += 1
                self.renew_during_people = False
            if path == "system/user/simple-list":
                people = [{"id": 257, "nickname": self.people_name},
                          {"id": 249, "nickname": "Current reviewer"}]
                if self.duplicate_people:
                    people.append({"id": 257, "nickname": "Later duplicate owner"})
                return people
            return [{"id": 7, "name": "Current department"}]
        if path == "crm/contract/page":
            self.calls.append((path, params))
            records = self.records if params.get("sceneType") == 1 else []
            offset = (params["pageNo"] - 1) * params["pageSize"]
            return {"list": records[offset:offset + params["pageSize"]], "total": len(records)}
        return super().get(path, **params)


class RowPeopleNamesTests(unittest.TestCase):
    def setUp(self):
        self.client = _PeopleClient()
        docs = deepcopy(DOCS)
        props = docs["components"]["schemas"]["Contract"]["properties"]
        props["reviewerUserId"] = _prop("integer", "审核人的用户编号")
        props["ownerDeptId"] = _prop("integer", "所属部门编号")
        self.data = YudaoGoalData(self.client, docs)
        self.data.schema()
        self.client.calls.clear()

    def calls(self, path):
        return sum(p == path for p, _ in self.client.calls)

    def test_many_rows_fields_and_filter_share_one_people_table_per_relation(self):
        rows = self.data.rows("crm/contract", Compare("ownerUserId", "eq", "257"),
                              fields=("ownerUserId", "reviewerUserId", "ownerDeptId"))
        self.assertEqual(len(rows), 64)
        self.assertEqual(self.calls("system/user/simple-list"), 1)
        self.assertEqual(self.calls("system/dept/simple-list"), 1)
        for row in rows:
            self.assertEqual(row.shown, {"ownerUserId": "Current owner",
                "reviewerUserId": "Current reviewer", "ownerDeptId": "Current department"})

    def test_duplicate_people_ids_retain_the_original_first_match(self):
        self.client.duplicate_people = True
        rows = self.data.rows("crm/contract", EVERYTHING, fields=("ownerUserId",))
        self.assertTrue(all(row.shown["ownerUserId"] == "Current owner" for row in rows))
        self.assertEqual(self.calls("system/user/simple-list"), 1)

    def test_next_rows_reads_people_again_and_retains_typed_revocation(self):
        for field, path in (("ownerUserId", "system/user/simple-list"),
                            ("ownerDeptId", "system/dept/simple-list")):
            with self.subTest(field=field):
                self.setUp()
                first = self.data.rows("crm/contract", EVERYTHING, fields=(field,))
                self.assertEqual(len(first), 64)
                self.assertEqual(self.calls(path), 1)
                self.client.denied.add(path)  # same credentials, permission changed upstream
                with self.assertRaises(DataRefused) as caught:
                    self.data.rows("crm/contract", EVERYTHING, fields=(field,))
                self.assertTrue(caught.exception.permission_denied)
                self.assertEqual(self.calls(path), 2)

    def test_next_rows_uses_new_names_even_while_primary_list_cache_is_live(self):
        first = self.data.rows("crm/contract", EVERYTHING, fields=("ownerUserId",))
        self.client.people_name = "Changed owner"
        second = self.data.rows("crm/contract", EVERYTHING, fields=("ownerUserId",))
        self.assertEqual(first[0].shown["ownerUserId"], "Current owner")
        self.assertEqual(second[0].shown["ownerUserId"], "Changed owner")
        self.assertEqual(self.calls("system/user/simple-list"), 2)

    def test_embedded_names_and_unrequested_fields_do_not_read_people(self):
        for record in self.client.records:
            record["ownerUserName"] = "Embedded owner"
        rows = self.data.rows("crm/contract", EVERYTHING, fields=("ownerUserId",))
        self.assertTrue(all(row.shown["ownerUserId"] == "Embedded owner" for row in rows))
        self.assertEqual(self.calls("system/user/simple-list"), 0)
        self.data.rows("crm/contract", EVERYTHING, fields=("totalPrice",))
        self.assertEqual(self.calls("system/dept/simple-list"), 0)

    def test_missing_people_ids_keep_existing_display_fallback_without_repeated_gets(self):
        for record in self.client.records:
            record["ownerUserId"] = 999
        rows = self.data.rows("crm/contract", EVERYTHING, fields=("ownerUserId",))
        self.assertEqual(self.calls("system/user/simple-list"), 1)
        self.assertTrue(all(row.values["ownerUserId"].id == "999" and
                            row.shown["ownerUserId"] == "999" for row in rows))

    def test_credential_change_during_read_refuses_local_old_actor_names(self):
        self.client.renew_during_people = True
        with self.assertRaises(DataRefused) as caught:
            self.data.rows("crm/contract", EVERYTHING, fields=("ownerUserId",))
        self.assertTrue(caught.exception.read_failed)


if __name__ == "__main__":
    unittest.main()
