"""Read facts keep their application meaning through the default answer contract."""
import unittest

from web_operation_agent.answer_protocol import compute, submit
from web_operation_agent.answer_view import render_payload
from web_operation_agent.business import BusinessToolContractError, BusinessToolRequestError
from web_operation_agent.computation import ComputationRefused
from web_operation_agent.evidence import RecordEvidence, TurnEvidence

from .test_adapter import _FakeOdoo, _customers, _orders, _quotations


def evidence(result):
    turn = TurnEvidence()
    turn.capture(result.evidence, namespace="call1")
    return turn


def field(turn, name, record_id=None):
    return next(item for ref in turn.ids()
                if isinstance(item := turn.get(ref), RecordEvidence)
                and item.field_name == name
                and (record_id is None or item.record_id == record_id))


class ApplicationReadMeaning(unittest.TestCase):
    def test_phone_is_read_and_empty_mobile_is_distinct_from_missing_field(self):
        rows = _customers([("Azure Interior", "azure@example.test")])
        rows[0].update(phone="(603)-996-3829", mobile=False)
        result = _FakeOdoo({"res.partner": rows}).execute("query_customers", {})
        turn = evidence(result)
        phone, mobile = field(turn, "phone"), field(turn, "mobile")
        self.assertEqual(phone.value, "(603)-996-3829")
        self.assertTrue(mobile.is_known_empty)
        self.assertEqual(result.content["customers"][0]["phone"], phone.value)

    def test_an_omitted_field_is_not_reported_as_empty(self):
        class Omitted(_FakeOdoo):
            def call(self, model, method, args, kwargs=None, *, mutation=False):
                result = super().call(model, method, args, kwargs)
                if method == "search_read":
                    for row in result:
                        row.pop("phone", None)
                return result
        turn = evidence(Omitted({"res.partner": _customers([("Acme", "a@example.test")])})
                        .execute("query_customers", {}))
        self.assertFalse(field(turn, "phone").field_was_read)
        self.assertFalse(field(turn, "phone").is_known_empty)

    def test_zero_money_retains_a_value_and_uses_the_records_own_currency(self):
        rows = _quotations([("Q1", "draft")])
        rows[0].update(amount_total=0, currency_id=[2, "EUR"])
        executor = _FakeOdoo({"sale.order": rows})
        turn = evidence(executor.execute("query_quotations", {}))
        amount = field(turn, "amount_total")
        self.assertEqual((amount.value, amount.unit), ("0", "EUR"))
        result = compute(turn, {"operation": "sum", "dataset_ref": "call1-query#1",
                               "field": "amount_total"}, result_ref="computed#1", observed_at="now")
        self.assertEqual((result.value, result.unit), ("0", "EUR"))
        self.assertFalse(any(call[0] == "res.company" for call in executor.calls))

    def test_different_currencies_cannot_be_summed_or_ranked_together(self):
        rows = _quotations([("Q1", "draft"), ("Q2", "draft")])
        rows[1]["currency_id"] = [2, "EUR"]
        turn = evidence(_FakeOdoo({"sale.order": rows}).execute("query_quotations", {}))
        for operation in ("sum", "argmax", "filter"):
            args = {"operation": operation, "dataset_ref": "call1-query#1", "field": "amount_total"}
            if operation == "filter":
                args.update(comparison="gt", threshold="100")
            with self.subTest(operation=operation), self.assertRaises(ComputationRefused):
                compute(turn, args, result_ref="computed#1", observed_at="now")

    def test_currency_filter_then_sum_is_valid(self):
        rows = _quotations([("Q1", "draft"), ("Q2", "draft")])
        rows[1]["currency_id"] = [2, "EUR"]
        turn = evidence(_FakeOdoo({"sale.order": rows}).execute("query_quotations", {}))
        compute(turn, {"operation": "filter", "dataset_ref": "call1-query#1",
                      "field": "currency_id", "comparison": "eq", "threshold": "EUR"},
                result_ref="computed#1", observed_at="now")
        result = compute(turn, {"operation": "sum", "dataset_ref": "computed#1",
                               "field": "amount_total"}, result_ref="computed#2", observed_at="now")
        self.assertEqual((result.value, result.unit), ("503.00", "EUR"))

    def test_a_missing_currency_is_not_replaced_by_a_company_currency(self):
        rows = _quotations([("Q1", "draft")])
        rows[0].pop("currency_id")
        with self.assertRaises(BusinessToolContractError):
            _FakeOdoo({"sale.order": rows}).execute("query_quotations", {})

    def test_catalog_price_precision_is_not_rounded_before_computation(self):
        row = {"id": 1, "name": "Part", "sale_ok": True, "list_price": 0.0125,
               "currency_id": [1, "USD"]}
        turn = evidence(_FakeOdoo({"product.template": [row]}).execute("query_products", {}))
        self.assertEqual(field(turn, "list_price").value, "0.0125")

    def test_not_found_names_the_name_filter_that_actually_ran(self):
        rows = _quotations([("Q1", "draft")])
        result = _FakeOdoo({"sale.order": rows}).execute("query_quotations", {"name_contains": "S00042"})
        answer = submit(evidence(result), {"outcome": "answered", "answer": {
            "version": 1, "coverage": "complete", "conclusions": [
                {"kind": "not_found", "evidence_ref": "call1-query#1"}]}})
        self.assertIn("S00042", render_payload(answer, user_text="谁的报价？", max_chars=4000))
        self.assertTrue(any("S00042" in condition for condition in result.content["conditions"]))

    def test_date_range_is_half_open_and_shared_by_count_and_read(self):
        """Boundaries are the signed-in user's days; with no zone configured they are UTC,
        which is what Odoo itself shows such a user (P7.5 CR-18)."""
        rows = _orders(4)
        dates = ["2026-08-31 23:59:59", "2026-09-01 00:00:00",
                 "2026-09-30 23:59:59", "2026-10-01 00:00:00"]
        for row, timestamp in zip(rows, dates):
            row["date_order"] = timestamp
        executor = _FakeOdoo({"sale.order": rows})
        result = executor.execute("query_sale_orders", {"date_from": "2026-09-01", "date_until": "2026-10-01"})
        self.assertEqual([row["reference"] for row in result.content["sale_orders"]], ["S00002", "S00003"])
        self.assertEqual(result.content["source_total"], 2)
        self.assertFalse(result.content["collection_ui_path_shows_this_scope"])
        domains = [call[2][0] for call in executor.calls if call[1].startswith("search")]
        self.assertTrue(all(domain == domains[0] for domain in domains))
        turn = evidence(result)
        # The meaning no longer claims UTC: the value is in the reader's own zone, as every
        # Odoo screen shows it, and saying UTC there was wrong for everyone who has one.
        self.assertNotIn("UTC", field(turn, "date_order").field_meaning)
        total = compute(turn, {"operation": "sum", "dataset_ref": "call1-query#1", "field": "amount_total"},
                        result_ref="computed#1", observed_at="now")
        self.assertIn("2026-10-01", total.origin.scope)

    def test_customer_filter_uses_the_relation_id_not_the_display_name(self):
        rows = _orders(2)
        rows[1]["partner_id"] = [4, "Acme"]  # same name, different record
        result = _FakeOdoo({"sale.order": rows}).execute("query_sale_orders", {"customer_id": 4})
        self.assertEqual([row["reference"] for row in result.content["sale_orders"]], ["S00002"])
        self.assertIn("partner_id = 4", result.content["conditions"])

    def test_invalid_or_inapplicable_filters_make_no_api_calls(self):
        cases = [("query_sale_orders", {"date_from": "2026-02-30"}),
                 ("query_sale_orders", {"date_from": "2026-09-01", "date_until": "2026-09-01"}),
                 ("query_sale_orders", {"customer_id": True}),
                 ("query_sale_orders", {"state": "cancel"}),
                 ("query_customers", {"date_from": "2026-09-01"}),
                 ("query_products", {"customer_id": 1})]
        executor = _FakeOdoo({})
        for tool, args in cases:
            with self.subTest(args=args), self.assertRaises(BusinessToolRequestError):
                executor.execute(tool, args)
        self.assertEqual(executor.calls, [])

    def test_invoice_amount_due_is_distinct_from_total_and_has_its_own_currency(self):
        row = {"id": 1, "name": "INV/1", "move_type": "out_invoice", "state": "posted",
               "amount_total": 100.0, "amount_residual": 25.0, "currency_id": [2, "EUR"],
               "invoice_date": "2026-09-01", "invoice_date_due": "2026-09-30"}
        result = _FakeOdoo({"account.move": [row]}).execute("query_customer_invoices", {"date_from": "2026-09-01"})
        turn = evidence(result)
        self.assertEqual(field(turn, "amount_residual").value, "25.00")
        self.assertEqual(field(turn, "amount_residual").unit, "EUR")
        self.assertEqual(field(turn, "amount_residual").field_meaning, "invoice amount due")
        self.assertEqual(field(turn, "amount_total").value, "100.00")

    def test_duplicate_or_missing_identity_cannot_make_a_scan_complete(self):
        class Broken(_FakeOdoo):
            def call(self, model, method, args, kwargs=None, *, mutation=False):
                if method == "search_count":
                    return 2
                if method == "search_read":
                    return self.tables[model]
                return super().call(model, method, args, kwargs)
        for rows in ([{"id": 1}, {"id": 1}], [{"id": True}], [{}], [None]):
            with self.subTest(rows=rows), self.assertRaises(BusinessToolContractError):
                Broken({"res.partner": rows}).execute("query_customers", {})


if __name__ == "__main__":
    unittest.main()
