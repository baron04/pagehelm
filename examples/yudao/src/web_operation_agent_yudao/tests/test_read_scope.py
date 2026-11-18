"""Synthetic scope regressions; no application, evaluation artifacts or model calls."""
import json
import unittest

from web_operation_agent.execution.read_summary import SummaryRefused
from web_operation_agent.tests.goal.test_data_read_tools import context
from web_operation_agent_yudao.reads.filter_semantics import FilterSemantics, LABELS
from web_operation_agent_yudao.tests.test_read_summary import knowledge


PERIOD = {"column": "orderDate", "op": "between", "value": ["2026-09-01", "2026-09-30"]}
APPROVED = {"column": "status", "op": "=", "value": 2}
MEASURES = [{"op": "count", "as": "n"}, {"op": "sum", "column": "price", "as": "total"}]


def native(rows, *, semantics=None, equalities=None):
    result = knowledge(rows)
    result._docs["components"]["schemas"]["Order"]["properties"].update({
        "customerId": {"type": "integer"}, "orderDate": {"type": "string", "format": "date"}})
    if semantics is not None:
        result.filter_semantics = FilterSemantics({"/admin-api/orders/page": semantics},
            equalities={"/admin-api/orders/page": equalities} if equalities else None)
    return result


def read(know, spec, **params):
    know.call_api("orders/page", {"pageNo": 1, "pageSize": 2, **params},
        summary=spec, actor_digest=context().principal.actor_authority_digest)
    return know.last_query_answer


class NativeScopeTests(unittest.TestCase):
    def test_ignored_native_customer_filter_is_enforced_across_pages_without_merging_names(self):
        # The existing offline client deliberately ignores native filters.
        know = native([{"id": 1, "name": "同名联系人", "customerId": 7},
                       {"id": 2, "name": "同名联系人", "customerId": 8},
                       {"id": 3, "name": "同名联系人", "customerId": 7}],
                      semantics={"customerId": LABELS["eq"]}, equalities={"customerId": "customerId"})
        result = read(know, {"columns": ["name"]}, customerId="7")
        self.assertTrue(result.complete)
        self.assertEqual((result.source_count, result.matched_count), (3, 2))
        self.assertEqual(result.matched_record_ids, ("1", "3"))
        self.assertEqual(result.rows, (("同名联系人",), ("同名联系人",)))
        self.assertEqual(result.spec.to_json()["where"], [{"column": "customerId", "op": "=", "value": 7}])
        self.assertEqual([c.name for c in result.source_columns], ["name", "customerId"])
        self.assertEqual(len([c for c in know.client.calls if c[0] == "orders/page"]), 2)

    def test_explicit_local_customer_filter_works_without_compiled_native_semantics(self):
        know = native([{"id": 1, "name": "甲", "customerId": 7},
                       {"id": 2, "name": "乙", "customerId": 8}])
        result = read(know, {"columns": ["name"], "where": [
            {"column": "customerId", "op": "=", "value": 7}]})
        self.assertEqual(result.matched_record_ids, ("1",))
        self.assertTrue(result.complete)

    def test_changing_customer_does_not_reuse_the_previous_scope_or_mutate_the_spec(self):
        know = native([{"id": 1, "name": "甲", "customerId": 7},
                       {"id": 2, "name": "乙", "customerId": 8}],
                      semantics={"customerId": LABELS["eq"]}, equalities={"customerId": "customerId"})
        spec = {"columns": ["name"]}
        first = read(know, spec, customerId=7)
        second = read(know, spec, customerId=8)
        self.assertEqual(first.matched_record_ids, ("1",))
        self.assertEqual(second.matched_record_ids, ("2",))
        self.assertEqual(second.spec.to_json()["where"], [{"column": "customerId", "op": "=", "value": 8}])
        self.assertEqual(spec, {"columns": ["name"]})

    def test_empty_customer_result_keeps_exact_native_and_local_predicates(self):
        know = native([], semantics={"customerId": LABELS["eq"]}, equalities={"customerId": "customerId"})
        result = read(know, {"measures": [{"op": "count", "as": "n"}]}, customerId=7)
        scope = {"native_filters": json.loads(result.origin.scope), "computation": result.spec.to_json()}
        self.assertEqual(scope["native_filters"], {"customerId": 7})
        self.assertEqual(scope["computation"]["where"], [{"column": "customerId", "op": "=", "value": 7}])
        self.assertEqual(result.rows, (("0",),))

    def test_missing_relation_value_cannot_certify_an_enforced_customer_subset(self):
        know = native([{"id": 1, "name": "未读到客户关系"}], semantics={"customerId": LABELS["eq"]},
                      equalities={"customerId": "customerId"})
        with self.assertRaises(SummaryRefused) as refused:
            read(know, {"columns": ["name"]}, customerId=7)
        self.assertEqual(refused.exception.code, "missing_column")
        self.assertIsNone(know.last_query_answer)

    def test_unproven_or_non_equality_native_semantics_are_not_guessed(self):
        for semantics in (None, {"customerId": LABELS["in"]}, {"customerId": LABELS["eq"]}):
            know = native([{"id": 1, "name": "甲", "customerId": 7},
                           {"id": 2, "name": "乙", "customerId": 8}], semantics=semantics)
            result = read(know, {"columns": ["name"]}, customerId=7)
            self.assertEqual(result.matched_count, 2)
            self.assertEqual(result.spec.where, ())
            self.assertEqual(json.loads(result.origin.scope), {"customerId": 7})

    def test_native_money_equality_is_not_compared_to_converted_display_money(self):
        # Only identity bindings are reapplied. Native cents must not become a
        # predicate on the summary's converted yuan values.
        know = native([{"id": 1, "price": 100}], semantics={"price": LABELS["eq"]}, equalities={"price": "price"})
        know.money_review = knowledge([], reviewed=True).money_review
        know._docs["paths"]["/admin-api/orders/page"]["get"]["parameters"].append(
            {"name": "price", "in": "query", "schema": {"type": "integer"}})
        result = read(know, {"measures": MEASURES}, price=100)
        self.assertEqual(result.rows, (("1", "1.00"),))
        self.assertEqual(result.spec.where, ())

    def test_period_and_explicit_approval_keep_distinct_populations(self):
        know = native([{"id": 1, "orderDate": "2026-09-04", "status": 0, "price": 100},
                       {"id": 2, "orderDate": "2026-09-15", "status": 2, "price": 200},
                       {"id": 3, "orderDate": "2026-08-10", "status": 2, "price": 400}])
        all_statuses = read(know, {"where": [PERIOD], "measures": MEASURES})
        approved_only = read(know, {"where": [PERIOD, APPROVED], "measures": MEASURES})
        self.assertEqual(all_statuses.rows, (("2", "300"),))
        self.assertEqual(approved_only.rows, (("1", "200"),))
        self.assertEqual(all_statuses.matched_record_ids, ("1", "2"))
        self.assertEqual(approved_only.matched_record_ids, ("2",))

