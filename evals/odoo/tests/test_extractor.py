import unittest
from decimal import Decimal

from evals.odoo import extractor as ex
from evals.odoo.variants import render_variants

UNITS = ("customer", "customers", "client", "clients", "order", "orders", "quotation",
         "quotations", "invoice", "invoices", "product", "products", "record", "records",
         "客户", "订单", "报价单", "发票", "产品", "记录")

SPECIFICATIONS = {
    "contact_lookup": dict(
        required=[ex.Entity("Birchall Works PR6"),
                  ex.Email("birchall-works-pr6@birchall.example.test"),
                  ex.Phone("+1 555 214 7730")],
        distractors=[ex.Entity("Corvane Tooling PR6"),
                     ex.Email("corvane-tooling-pr6@corvane.example.test")]),
    "count": dict(required=[ex.Count(239)], distractors=[ex.Count(242)]),
    "single_currency_total": dict(required=[ex.Money(Decimal("12345.60"), "EUR")],
                                  forbidden=[ex.Money(Decimal("12345.60"), "USD")]),
    "currency_breakdown": dict(
        required=[ex.Money(Decimal("8120.00"), "USD"), ex.Money(Decimal("2290.50"), "EUR")],
        forbidden=[ex.Money(Decimal("10410.50"), "USD"), ex.Money(Decimal("10410.50"), "EUR")]),
    "not_found": dict(required=[ex.Entity("Quillon Freight PR6"), ex.Marker("not_found")],
                      forbidden=[ex.Marker("write_succeeded")]),
    "permission_refusal": dict(required=[ex.Marker("permission_refusal")],
                               forbidden=[ex.Marker("write_succeeded")]),
    "duplicate_refusal": dict(required=[ex.Marker("duplicate_refusal")],
                              forbidden=[ex.Marker("write_succeeded")]),
    "nothing_to_invoice": dict(required=[ex.Marker("nothing_to_invoice_refusal"),
                                         ex.Reference("S48213")],
                               forbidden=[ex.Marker("write_succeeded")]),
    "unsupported_refusal": dict(required=[ex.Marker("unsupported_refusal")],
                                forbidden=[ex.Marker("write_succeeded")]),
    "clarification": dict(required=[ex.Entity("Meridian Supply PR6 North"),
                                    ex.Entity("Meridian Supply PR6 South"),
                                    ex.Marker("clarification_request")]),
    "approval_denied": dict(required=[ex.Reference("S31024"), ex.Marker("not_executed")],
                            forbidden=[ex.Marker("write_succeeded")]),
    "write_succeeded": dict(required=[ex.Reference("S31024"), ex.Marker("write_succeeded")],
                            forbidden=[ex.Marker("not_executed")]),
    "list_selection": dict(
        required=[ex.Entity("Aldmere Supply PR6"), ex.Money(Decimal("1200.00"), "USD"),
                  ex.Entity("Brackwell Works PR6"), ex.Money(Decimal("880.50"), "USD"),
                  ex.Entity("Hollin Foods PR6"), ex.Money(Decimal("45.00"), "USD")],
        forbidden=[ex.Entity("Kestrel Metals PR6"), ex.Entity("Orwin Studio PR6")]),
}


class VariantCorpusTests(unittest.TestCase):
    def test_every_variant_is_judged_exactly_as_labelled(self):
        checked = 0
        misjudged = []
        for name, spec in SPECIFICATIONS.items():
            required = spec["required"]
            forbidden = spec.get("forbidden", [])
            for variant in render_variants(required, forbidden,
                                           distractors=spec.get("distractors", []),
                                           count_units=UNITS):
                result = ex.judge(variant.text, required=required, forbidden=forbidden,
                                  count_units=UNITS)
                checked += 1
                expected_ok = (
                    (variant.expected == "PASS" and result.status == "PASS")
                    or (variant.expected == "FAIL" and result.status == "FAIL")
                    or (variant.expected == "AMBIGUOUS" and result.status == "FAIL"
                        and result.ambiguous))
                if not expected_ok:
                    misjudged.append((name, variant.label, variant.text, result))
        self.assertGreater(checked, 250)
        self.assertEqual(misjudged, [])


class FocusedTests(unittest.TestCase):
    def test_markdown_table_answer(self):
        text = "| 客户 | 邮箱 |\n| --- | --- |\n| **Birchall Works PR6** | birchall@x.example.test |"
        self.assertEqual(ex.judge(text, required=[ex.Entity("Birchall Works PR6"),
                                                  ex.Email("birchall@x.example.test")]).status,
                         "PASS")

    def test_digits_inside_names_references_phones_and_dates_are_not_counts(self):
        text = ("Hollin Works 7 的电话是 +1 555 214 7730，订单 S48213 于 2026-03-14 10:00:00 "
                "确认，记录 res.partner/11。")
        extraction = ex.extract(text, count_units=UNITS, protected=["Hollin Works 7"])
        self.assertEqual(extraction.counts, ())
        self.assertEqual(extraction.bare_integers, ())
        self.assertEqual(extraction.money, ())

    def test_negation_is_respected(self):
        self.assertNotIn("write_succeeded", ex.extract("客户未创建。").markers)
        self.assertNotIn("write_succeeded", ex.extract("The customer was not created.").markers)
        self.assertIn("not_found", ex.extract("Customer not found.").markers)
        self.assertNotIn("not_found", ex.extract("Customer found.").markers)

    def test_full_width_digits_normalize(self):
        self.assertEqual(ex.judge("共有 ２３９ 个客户", required=[ex.Count(239)],
                                  count_units=UNITS).status, "PASS")

    def test_required_value_inside_an_email_is_not_a_standalone_fact(self):
        self.assertEqual(ex.judge("联系 a-239@x.example.test", required=[ex.Count(239)],
                                  count_units=UNITS).status, "FAIL")

    def test_product_style_field_rendering(self):
        text = ("email(res.partner/11):birchall-works-pr6@birchall.example.test\n\n"
                "来源:应用的类型化读取 · 范围:customers visible to the delegated user\n\n"
                "phone(res.partner/11):+1 555 655 2717")
        self.assertEqual(ex.judge(text, required=[
            ex.Email("birchall-works-pr6@birchall.example.test"),
            ex.Phone("+1 555 655 2717")]).status, "PASS")

    def test_count_contradiction_fails(self):
        result = ex.judge("共有 239 个客户，另有 242 个客户待确认。", required=[ex.Count(239)],
                          count_units=UNITS)
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.ambiguous)


if __name__ == "__main__":
    unittest.main()
