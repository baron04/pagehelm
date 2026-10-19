"""Native schema/actor/display proofs feed the common calculator, without model calls."""
import copy
from decimal import Decimal
import unittest
from types import SimpleNamespace as NS

from web_operation_agent.execution.read_summary import SummaryRefused
from web_operation_agent.state.identity import actor_authority_digest
from web_operation_agent.tests.execution.test_product_model_loop import LoopFixture, call, reply
from web_operation_agent.tests.goal.test_data_read_tools import context
from web_operation_agent_yudao.money_review import VerifiedMoneyField, VerifiedMoneyReview
from web_operation_agent_yudao.slim_loop import ActorKnowledge, QueryParametersError, YudaoLoopProcedures
from web_operation_agent_yudao.tests.test_slim_loop import Client, DOCS, State


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

    def test_native_detail_missing_identity_foreign_identity_and_unreviewed_money_are_refused(self):
        for returned in ({"name": "no id"}, {"id": 9, "name": "wrong record"}, {"id": 8, "price": 100}):
            know = knowledge([])
            original = know.client.get
            know.client.get = lambda path, **params: returned if path == "orders/get" else original(path, **params)
            with self.assertRaises(SummaryRefused):
                know.call_api("orders/get", {"id": 8}, summary={"columns": ["price" if "price" in returned else "name"]},
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

    def test_unreviewed_money_and_redacted_or_unpublished_columns_never_bypass_original_proofs(self):
        for field, row, code in (("price", {"price": 100}, "unverified_money"),
                ("apiSecret", {"apiSecret": "secret"}, "redacted_value"),
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

    def test_invalid_argument_combinations_are_refused_before_reading(self):
        for extra in ({"deliver": True}, {"row_offset": 1}):
            know = knowledge([])
            with self.assertRaises(QueryParametersError):
                self.read(know, ["name"], COUNT, **extra)
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


if __name__ == "__main__":
    unittest.main()
