"""Native linked prices require actor-bound exact product reads, never guesses."""
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from web_operation_agent.execution.draft_handoff import field_identity, handoff_draft
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent_yudao.draft_readback import product_price_readback


class ProductPriceReadbackTests(unittest.TestCase):
    def test_only_exact_unique_product_and_reviewed_price_can_change(self):
        attrs = {"form_scope": "dialog:x", "form_instance": "live"}
        fields = [ElementRef("e1", "combobox", "", "input", 1,
                    {**attrs, "title": "明细第1行·产品名称", "selected_option": ""}),
                  ElementRef("e2", "spinbutton", "", "input", 1,
                    {**attrs, "title": "明细第1行·产品单价", "value": ""})]
        before = PageObservation(1, 1, "http://app.test/erp/sale/order", "", [], fields, "",
                                 page_route="/erp/sale/order", render_state="ready")
        after = replace(before, epoch=2, elements=[replace(e, epoch=2, attrs={**e.attrs,
            **({"selected_option": "Exact product"} if e.ref == "e1" else {"value": "12.50"})}) for e in fields])
        expected = [(field_identity(fields[0]), "Exact product")]
        context = NS(obs=after, operation=object(), page=NS(checkpoint=lambda: None), audit=lambda *a, **k: None)
        for rows, valid in [([{"name": "Exact product", "salePrice": 12.5}], True),
                            ([{"name": "Exact product", "salePrice": 99}], False),
                            ([{"name": "Exactproduct", "salePrice": 12.5}], False),
                            ([{"name": "Exact product", "salePrice": 12.5}] * 2, False)]:
            with self.subTest(rows=rows), patch("web_operation_agent_yudao.draft_readback.BoundDataReads") as bound:
                bound.return_value._invoke.return_value = rows
                result = product_price_readback(context, before, expected, object())
                self.assertEqual(2 if valid else 1, len(result))
                if valid: self.assertEqual((field_identity(fields[1]), "12.5"), result[-1])
        with patch("web_operation_agent_yudao.draft_readback.BoundDataReads") as bound:
            self.assertEqual(expected, product_price_readback(context, replace(before, page_route="/other"), expected, object()))
            self.assertEqual([], product_price_readback(context, before, [], object()))
            bound.assert_not_called()
