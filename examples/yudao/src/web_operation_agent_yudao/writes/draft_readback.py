"""Readback of reviewed product-price coupling in explicit-save ERP drafts."""
import re

from web_operation_agent.execution.draft_handoff import draft_fields, field_identity, field_value, value_matches
from ..reads.bound_data_reads import BoundDataReads


def product_price_readback(context, before, expected, data):
    """Only the selected row's native product price may replace an untouched value.

    SaleOrderItemForm.onChangeProduct copies salePrice; StockInItemForm copies
    minPrice. Neither a model-provided price nor an unrelated changed field is
    admitted by this check. Other routes retain the strict unchanged-field check.
    """
    price_key = {"/erp/sale/order": "salePrice", "/erp/stock/in": "minPrice"}.get(before.page_route)
    if price_key is None:
        return expected
    old = {field_identity(e): e for e in draft_fields(before)}
    wanted, changes = dict(expected), []
    for element in draft_fields(context.obs):
        key = field_identity(element)
        row = re.fullmatch(r"明细第([1-9][0-9]*)行·产品单价", element.attrs.get("title", ""))
        if (key in wanted or key not in old or not row
                or field_value(old[key]) == field_value(element)):
            continue
        product_key = (*key[:5], f"明细第{row[1]}行·产品名称")
        # Role differs between the combobox and numeric price.
        product_key = (*product_key[:2], "combobox", *product_key[3:])
        if product_key in wanted:
            changes.append((element, wanted[product_key]))
    if not changes:
        return expected
    bound = BoundDataReads(data, context.operation, context.page.checkpoint)
    products = bound._invoke(bound._client.get, "erp/product/simple-list")
    if not isinstance(products, list):
        return expected
    extra = []
    for element, name in changes:
        matches = [p for p in products if isinstance(p, dict) and p.get("name") == name]
        if len(matches) == 1 and price_key in matches[0]:
            value = str(matches[0][price_key])
            if value_matches(element, value):
                extra.append((field_identity(element), value))
    context.audit("draft_linked_values_readback", verified_fields=len(extra), submitted=False, persisted=False)
    return [*expected, *extra]
