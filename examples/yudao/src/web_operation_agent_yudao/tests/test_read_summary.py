"""Native schema/actor/display proofs feed the common calculator, without model calls."""
import copy
from decimal import Decimal
import unittest
from types import SimpleNamespace as NS

from web_operation_agent.execution.read_summary import SummaryRefused, render_summary
from web_operation_agent.state.identity import actor_authority_digest
from web_operation_agent.tests.execution.test_product_model_loop import LoopFixture, call, reply
from web_operation_agent.tests.goal.test_data_read_tools import context
from web_operation_agent_yudao.reads.money_review import VerifiedMoneyField, VerifiedMoneyReview
from web_operation_agent_yudao.executor.slim_loop import ActorKnowledge, QueryParametersError, YudaoLoopProcedures
from web_operation_agent_yudao.tests.slim_loop_support import Client, DOCS, State


class Rows(Client):
    def __init__(self, rows, *, repeated=False):
        super().__init__()
        self.rows, self.repeated = rows, repeated

    def get(self, path, **params):
        if path == "orders/get":
            self.calls.append((path, params))
            return next((row for row in self.rows if row.get("id") == params.get("id")), None)
        if path != "orders/page":
            return super().get(path, **params)
        self.calls.append((path, params))
        page, size = params.get("pageNo", 1), params.get("pageSize", 20)
        start = 0 if self.repeated else (page - 1) * size
        return {"total": len(self.rows), "list": self.rows[start:start + size]}


def knowledge(rows, *, reviewed=False, repeated=False):
    docs = copy.deepcopy(DOCS)
    docs["components"]["schemas"]["Order"]["properties"] = {
        "id": {"type": "integer"}, "name": {"type": "string"},
        "deptId": {"type": "integer"}, "quantity": {"type": "number"},
        "deptName": {"type": "string"},
        "status": {"type": "integer"}, "price": {"type": "integer", "description": "单价，单位：分"},
        "created": {"type": "string", "format": "date-time"},
        "createTime": {"type": "integer", "format": "int64"},
        "dueDate": {"type": "string", "format": "date"},
        "apiSecret": {"type": "string"},
    }
    docs["paths"]["/admin-api/orders/get"] = {"get": {"summary": "获得订单", "tags": ["管理后台 - 订单"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}],
        "responses": {"200": {"content": {"application/json": {"schema": {"type": "object", "properties": {
            "code": {"type": "integer"}, "data": {"$ref": "#/components/schemas/Order"}}}}}}}}}
    policy = VerifiedMoneyReview((VerifiedMoneyField("/orders", "orders/page", "price", Decimal("0.01"), "元"),)) if reviewed else None
    return ActorKnowledge(Rows(rows, repeated=repeated), docs, money_review=policy)


COUNT = {"measures": [{"op": "count", "as": "records"}]}


class NativeSummaryTests(unittest.TestCase):
    def read(self, know, fields, spec, **kwargs):
        know.call_api("orders/page", {"pageNo": 1, "pageSize": 2}, fields,
            summary=spec, actor_digest=actor_authority_digest("app", "tenant", "actor"), **kwargs)
        return know.last_query_answer

    def test_native_dictionary_display_keeps_raw_numeric_codes_and_unknowns(self):
        know = knowledge([{"id":1,"status":10}, {"id":2,"status":2}, {"id":3,"status":0}, {"id":4,"status":None}])
        know._columns = lambda path: {"status":"状态"}
        know._codes = lambda path: {"status":{"2":"同名状态", "10":"同名状态"}}
        result = self.read(know, [], {"columns":["status"], "order_by":[{"column":"status","direction":"asc"}]})
        self.assertEqual((("0",),("2",),("10",),(None,)), result.rows)
        self.assertEqual("number", result.columns[0].kind)
        self.assertEqual("状态",result.columns[0].display_name)
        rendered = render_summary(result,max_chars=5000,user_text="查看状态").text
        self.assertIn("0（字典中无此代码）",rendered)
        self.assertIn("2（同名状态）",rendered)
        self.assertIn("10（同名状态）",rendered)
        self.assertEqual({"status":1},dict(result.null_counts))
        self.assertEqual([["2","同名状态"],["10","同名状态"]], result.to_json()["columns"][0]["labels"])
        grouped = self.read(know,[],{"group_by":["status"],**COUNT})
        self.assertEqual((("10",),("2",),("0",),(None,)),grouped.group_keys)
        filtered = self.read(know,[],{"columns":["status"],"where":[{"column":"status","op":">","value":2}]})
        self.assertEqual((("10",),),filtered.rows)
        total = self.read(know,[],{"measures":[{"op":"sum","column":"status","as":"n"}]})
        self.assertEqual((("12",),),total.rows)
        self.assertIsNone(total.columns[0].labels)

    def test_native_display_without_dictionary_does_not_invent_unknown_codes(self):
        know = knowledge([{"id":1,"status":0,"deptId":10,"deptName":"部门"}])
        result = self.read(know,[],{"columns":["status","deptId"]})
        self.assertIsNone(result.columns[0].labels)
        self.assertEqual("relation",result.columns[1].kind)
        self.assertEqual((("0","部门 (10)"),),result.rows)
        self.assertNotIn("字典中无此代码",render_summary(result,max_chars=4000,user_text="状态").text)

    def test_complete_native_record_sorting_preserves_all_pages_and_hidden_sort_column(self):
        know = knowledge([{"id":i+1,"name":f"Record {i}","quantity":i} for i in range(75)])
        result = self.read(know, [], {"columns":["name"],"order_by":[{"column":"quantity","direction":"desc"}]})
        self.assertTrue(result.complete)
        self.assertEqual(75,len(result.rows))
        self.assertEqual((("Record 74",),("Record 73",)),result.rows[:2])
        self.assertEqual(("Record 0",),result.rows[-1])
        self.assertEqual(("75",),result.row_record_ids[0])
        self.assertEqual(("name",),tuple(c.name for c in result.columns))

    def test_prefix_and_status_are_both_applied_to_exact_decimals(self):
        know = knowledge([
            {"id": 1, "name": "测试甲", "status": 0, "quantity": 0.1},
            {"id": 2, "name": "测试乙", "status": 0, "quantity": 0.2},
            {"id": 3, "name": "其它", "status": 0, "quantity": 100},
            {"id": 4, "name": "测试丙", "status": 1, "quantity": 100},
        ])
        result = self.read(know, ["name", "status", "quantity"], {
            "where": [{"column": "name", "op": "prefix", "value": "测试"},
                      {"column": "status", "op": "=", "value": 0}],
            "measures": [{"op": "sum", "column": "quantity", "as": "total"}]})
        self.assertTrue(result.complete)
        self.assertEqual((4, 2, (("0.3",),)), (result.source_count, result.matched_count, result.rows))
        self.assertIn("同一时点", result.consistency)
        self.assertEqual(result.matched_record_ids, ("1", "2"))

    def test_summary_derives_all_needed_columns_without_a_second_fields_declaration(self):
        know = knowledge([{"id": 1, "name": "前缀甲", "status": 0},
                          {"id": 2, "name": "其它", "status": 0}])
        result = self.read(know, (), {**COUNT, "where": [{"column": "name", "op": "prefix", "value": "前缀"}]})
        self.assertEqual(result.rows, (("1",),))
        self.assertEqual(tuple(c.name for c in result.source_columns), ("name",))
        empty = self.read(knowledge([]), (), COUNT)
        self.assertEqual(empty.rows, (("0",),))
        self.assertEqual(tuple(c.name for c in empty.source_columns), ("id",))

    def test_native_detail_projection_preserves_record_identity_and_actual_field_value(self):
        know = knowledge([{"id": 8, "name": "exact | *value*", "quantity": 0.3}])
        know.call_api("orders/get", {"id": 8}, summary={"columns": ["name", "quantity"]},
                      actor_digest=actor_authority_digest("app", "tenant", "actor"))
        result = know.last_query_answer
        self.assertEqual(result.matched_record_ids, ("8",))
        self.assertEqual(result.rows, (("exact | *value*", "0.3"),))
        self.assertTrue(result.complete)
        self.assertEqual(result.source_count, 1)
        self.assertEqual(know.client.calls[-1], ("orders/get", {"id": 8}))

    def test_native_detail_missing_identity_and_foreign_identity_are_refused(self):
        for returned in ({"name": "no id"}, {"id": 9, "name": "wrong record"}):
            know = knowledge([])
            original = know.client.get
            know.client.get = lambda path, **params: returned if path == "orders/get" else original(path, **params)
            with self.assertRaises(SummaryRefused):
                know.call_api("orders/get", {"id": 8}, summary={"columns": ["name"]},
                              actor_digest=actor_authority_digest("app", "tenant", "actor"))

    def test_group_labels_come_from_same_native_foreign_key_name_pair_and_never_merge_ids(self):
        rows = [{"id": 1, "deptId": 10, "deptName": "同名部门"},
                {"id": 2, "deptId": 10, "deptName": "同名部门"},
                {"id": 3, "deptId": 20, "deptName": "同名部门"}]
        result = self.read(knowledge(rows), (), {"group_by": ["deptId"], **COUNT})
        self.assertEqual(result.rows, (("同名部门 (10)", "2"), ("同名部门 (20)", "1")))
        self.assertEqual(result.group_keys, (("10",), ("20",)))
        self.assertEqual(result.row_record_ids, (("1", "2"), ("3",)))
        rows[1]["deptName"] = "另一个名称"
        with self.assertRaises(SummaryRefused) as caught:
            self.read(knowledge(rows), (), {"group_by": ["deptId"], **COUNT})
        self.assertEqual(caught.exception.code, "ambiguous_relation_label")

    def test_money_ranking_uses_reviewed_exact_display_values_and_keeps_cutoff_ties(self):
        result = self.read(knowledge([
            {"id": 1, "name": "同名", "price": 101},
            {"id": 2, "name": "同名", "price": 99},
            {"id": 3, "name": "其它", "price": 99}], reviewed=True), ["id", "name", "price"], {
            "group_by": ["id"], "columns": ["name"],
            "measures": [{"op": "max", "column": "price", "as": "highest"}],
            "top": {"by": "highest", "n": 2, "direction": "desc"}})
        self.assertEqual(len(result.rows), 3)
        self.assertEqual([row[-1] for row in result.rows], ["1.01", "0.99", "0.99"])
        self.assertEqual(result.columns[-1].unit, "元")
        self.assertEqual(result.ties_at_cutoff, 2)

    def test_unreviewed_money_keeps_its_raw_value_marked_and_never_takes_a_unit(self):
        # Recorded turn Q003: one unreviewed amount refused the whole summary, and the
        # model answered the amount in free text with a unit no review had proven.
        result = self.read(knowledge([{"id": 1, "price": 100}]), ["price"], {"columns": ["price"]})
        self.assertEqual((result.rows, result.columns[0].unit, result.columns[0].unverified_unit), ((("100",),), "", True))
        self.assertIn("单位未核验", render_summary(result, max_chars=4000, user_text="单价").text)
        reviewed = self.read(knowledge([{"id": 1, "price": 100}], reviewed=True), ["price"], {"columns": ["price"]})
        self.assertEqual((reviewed.rows, reviewed.columns[0].unit, reviewed.columns[0].unverified_unit), ((("1.00",),), "元", False))

    def test_redacted_or_unpublished_columns_never_bypass_original_proofs(self):
        for field, row, code in (("apiSecret", {"apiSecret": "secret"}, "redacted_value"),
                ("unknown", {"unknown": "actual"}, "unpublished_column")):
            with self.subTest(field=field), self.assertRaises(SummaryRefused) as caught:
                self.read(knowledge([{"id": 1, **row}]), [field], {"columns": [field]})
            self.assertEqual(caught.exception.code, code)

    def test_repeated_pages_or_a_read_starting_after_page_one_cannot_be_complete(self):
        know = knowledge([{"id": i, "name": str(i)} for i in range(4)], repeated=True)
        result = self.read(know, ["name"], COUNT)
        self.assertFalse(result.complete)
        self.assertEqual(result.rows, ())
        know = knowledge([{"id": i, "name": str(i)} for i in range(4)])
        know.call_api("orders/page", {"pageNo": 2, "pageSize": 2}, ["name"],
            summary=COUNT, actor_digest="a" * 64)
        self.assertFalse(know.last_query_answer.complete)

    def test_money_empty_zero_and_null_are_distinct(self):
        spec = {"measures": [{"op": "sum", "column": "price", "as": "total"}]}
        for rows, expected in (([], "0"), ([{"id": 1, "price": 0}], "0.00"),
                               ([{"id": 1, "price": None}], None)):
            with self.subTest(rows=rows):
                result = self.read(knowledge(rows, reviewed=True), ["price"], spec)
                self.assertTrue(result.complete)
                self.assertEqual(result.rows[0][0], expected)

    def test_dates_are_read_in_the_application_zone_and_compared_as_instants(self):
        result = self.read(knowledge([{"id": 1, "created": "2026-10-18T00:00:00"}]), ["created"], {
            **COUNT, "where": [{"column": "created", "op": ">=", "value": "2026-10-17T16:00:00Z"}]})
        self.assertEqual(result.rows, (("1",),))
        with self.assertRaises(SummaryRefused) as caught:
            self.read(knowledge([{"id": 1, "created": "2026-10-18T00:00:00"}]), ["created"], {
                **COUNT, "where": [{"column": "created", "op": ">=", "value": "2026-10-18T00:00:00"}]})
        self.assertEqual(caught.exception.code, "type_mismatch")

    def test_native_epoch_milliseconds_cannot_be_summed_as_a_quantity(self):
        rows = [{"id": 1, "createTime": 1790784000123}]
        result = self.read(knowledge(rows), ["createTime"], {**COUNT,
            "where": [{"column": "createTime", "op": ">=", "value": "2026-09-30T16:00:00.123Z"}]})
        self.assertEqual(result.rows, (("1",),))
        with self.assertRaises(SummaryRefused) as caught:
            self.read(knowledge(rows), ["createTime"], {
                "measures": [{"op": "sum", "column": "createTime", "as": "total"}]})
        self.assertEqual(caught.exception.code, "type_mismatch")

    def test_compact_dates_and_redacted_text_collisions_are_not_normalized_into_facts(self):
        with self.assertRaises(SummaryRefused) as caught:
            self.read(knowledge([{"id": 1, "dueDate": "20261018"}]), ["dueDate"], {"columns": ["dueDate"]})
        self.assertEqual(caught.exception.code, "invalid_date")
        rows = [{"id": 1, "name": "token=alpha123"}, {"id": 2, "name": "token=beta456"}]
        with self.assertRaises(SummaryRefused) as caught:
            self.read(knowledge(rows), ["name"], {
                "measures": [{"op": "count_distinct", "column": "name", "as": "distinct"}]})
        self.assertEqual(caught.exception.code, "redacted_value")

    def test_summary_offsets_remain_refused(self):
        know = knowledge([])
        self.assertEqual((("0",),), self.read(know, ["name"], COUNT).rows)
        know = knowledge([])
        with self.assertRaises(QueryParametersError):
            self.read(know, ["name"], COUNT, row_offset=1)
        self.assertEqual(know.client.calls, [])

    def test_native_procedure_preserves_actor_bound_result_for_common_finish_delivery(self):
        procedure = YudaoLoopProcedures(knowledge([{"id": 1, "name": "实际名称"}]), None, "https://synthetic.test")
        state = State()
        state.operation = context()
        result = procedure._tool(state, "call_api", {
            "path": "orders/page", "params": '{"pageNo":1,"pageSize":20}',
            "fields": ["name"], "summary": {"columns": ["name"]}}, {"reads": []})
        self.assertEqual(result.query_answer.actor_digest, state.operation.principal.actor_authority_digest)
        loop = LoopFixture([reply(call("read")), reply(call("finish", status="done", answer="", read_answer="read-1"))],
            lambda *_: result)
        loop.state.approvals = NS(principal=state.operation.principal)
        self.assertIn("实际名称", loop.run())



class Departments(Client):
    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "system/dept/list":
            return [{"id": 100, "name": "研发部门", "leaderUserId": 1, "sort": 1, "status": 0},
                    {"id": 101, "name": "市场部门", "leaderUserId": None, "sort": 2, "status": 0},
                    {"id": 102, "name": "财务部门", "leaderUserId": 103, "sort": 3, "status": 1}]
        if path == "system/user/simple-list":
            return [{"id": 1, "nickname": "芋道源码"}, {"id": 103, "nickname": "源码"}, {"id": 104, "nickname": "测试"}]
        return super().get(path, **params)


def departments():
    docs = copy.deepcopy(DOCS)
    docs["paths"]["/admin-api/system/dept/list"] = {"get": {"summary": "获取部门列表", "tags": ["管理后台 - 部门"],
        "parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object", "properties": {
            "code": {"type": "integer"}, "data": {"type": "array", "items": {"$ref": "#/components/schemas/Dept"}}}}}}}}}}
    docs["components"]["schemas"]["Dept"] = {"properties": {
        "id": {"type": "integer"}, "name": {"type": "string"}, "leaderUserId": {"type": "integer"},
        "sort": {"type": "integer"}, "status": {"type": "integer"}}}
    return ActorKnowledge(Departments(), docs)


class ReadDeclarationTests(unittest.TestCase):
    actor = actor_authority_digest("app", "tenant", "actor")

    def test_the_recorded_count_declarations_compute_and_say_how_they_were_read(self):
        # Q029 sent `count` with `column: id` and a Chinese alias 22 times; Q039 a Chinese alias.
        know = knowledge([{"id": 1, "status": 0}, {"id": 2, "status": 0}, {"id": 3, "status": 1}])
        text = know.call_api("orders/page", {"pageNo": 1, "pageSize": 2}, (), summary={
            "measures": [{"op": "count", "column": "id", "as": "开启仓库数"}],
            "where": [{"column": "status", "op": "=", "value": "0"}]}, actor_digest=self.actor)
        self.assertEqual(know.last_query_answer.rows, (("2",),))
        self.assertIn("已改为 m1", text)

    def test_department_leaders_are_named_through_the_user_list_the_actor_reads(self):
        know = departments()
        text = know.call_api("system/dept/list", {}, ("name", "leaderUserId"), summary={
            "columns": ["name", "leaderUserId"], "order_by": [{"column": "sort", "direction": "asc"}]},
            actor_digest=self.actor)
        result = know.last_query_answer
        self.assertEqual(result.rows, (("研发部门", "芋道源码 (1)"), ("市场部门", None), ("财务部门", "源码 (103)")))
        self.assertEqual(result.columns[1].display_name, "负责人")
        self.assertIn("1=芋道源码", text)
        self.assertNotIn("测试", text)
        self.assertIn(("system/user/simple-list", {}), know.client.calls)


if __name__ == "__main__":
    unittest.main()
