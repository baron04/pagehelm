"""Deterministic WOA and PageAgent programs used by qualification."""
from __future__ import annotations

from collections.abc import Callable

from evals.odoo.execution import scripted as S
from evals.odoo.execution.harness import Context

def woa(turns: dict[str, tuple]) -> Callable:
    return S.WoaProgram(turns).policy()


def c_program(ctx: Context, task_id: str, *, variant: str = "") -> Callable:
    runner, _ = ctx.inputs(task_id)
    first = runner.messages[0]
    if task_id == "d01":
        name = ctx.record("customer.lookup")["name"]
        fields = ["phone"] if variant == "missing_field" else ["email", "phone"]
        return woa({first: (S.load_skill_with("query_customers"),
                            S.call("query_customers", {"name_contains": name}),
                            S.finish_fields(name, fields))})
    if task_id == "d02":
        name = ctx.record("customer.lookup_page")["name"]
        return woa({first: (S.load_skill_with("query_customers"),
                            S.call("query_customers", {"name_contains": name}),
                            S.goto_route_of(name),
                            S.finish_acted(f"已打开客户 {name} 的详情页。"))})
    if task_id == "d07":
        return woa({first: (S.load_skill_with("query_quotations"),
                            S.call("query_quotations", {"limit": 50}),
                            S.compute("filter", field="state", comparison="eq",
                                      threshold="sent"),
                            S.compute("count", over="SelectionEvidence"),
                            S.finish_aggregate())})
    if task_id == "d08":
        if variant == "no_currency_filter":
            # Drops the task's currency qualifier: sums every open quotation.
            return woa({first: (S.load_skill_with("query_quotations"),
                                S.call("query_quotations", {"limit": 50}),
                                S.compute("sum", field="amount_total"),
                                S.finish_aggregate())})
        return woa({first: (S.load_skill_with("query_quotations"),
                            S.call("query_quotations", {"limit": 50}),
                            S.compute("filter", field="currency_id", comparison="eq",
                                      threshold="EUR"),
                            S.compute("sum", field="amount_total", over="SelectionEvidence"),
                            S.finish_aggregate())})
    if task_id == "d11":
        prefix = ctx.population["roles"]["customer.ambiguous_prefix"]
        choice = ctx.record("customer.ambiguous_b")["name"]
        return woa({first: (S.load_skill_with("query_customers"),
                            S.call("query_customers", {"name_contains": prefix}),
                            S.ask_choices("请选择要打开的客户。")),
                    runner.messages[1]: (S.load_skill_with("query_customers"),
                                         S.call("query_customers", {"name_contains": choice}),
                                         S.goto_route_of(choice),
                                         S.finish_acted(f"已打开客户 {choice}。"))})
    if task_id == "d13":
        order = ctx.record("order.related")["name"]
        customer = ctx.record("customer.related_order")["name"]
        return woa({first: (S.load_skill_with("query_sale_orders"),
                            S.call("query_sale_orders", {"name_contains": order}),
                            S.finish_fields(order, ["partner_id"])),
                    runner.messages[1]: (S.load_skill_with("query_customers"),
                                         S.call("query_customers", {"name_contains": customer}),
                                         S.finish_fields(customer, ["city"]))})
    if task_id in {"d16", "d19"}:
        role = "new.create_customer" if task_id == "d16" else "new.missing_email"
        record = ctx.population["new_records"][role]
        return woa({first: (S.load_skill_with("create_customer", writes=True),
                            S.call("create_customer", {"name": record["name"],
                                                       "email": record["email"]}),
                            S.finish_acted(f"已创建客户 {record['name']}，邮箱 "
                                           f"{record['email']}。"))})
    if task_id == "d18":
        reference = ctx.record("order.to_invoice")["name"]
        return woa({first: (S.load_skill_with("invoice_sale_order", writes=True),
                            S.call("invoice_sale_order", {"reference": reference}),
                            S.finish_acted(f"已为销售订单 {reference} 创建客户发票。"))})
    if task_id == "d20":
        reference = ctx.record("quotation.deny")["name"]
        return woa({first: (S.load_skill_with("confirm_quotation", writes=True),
                            S.call("confirm_quotation", {"reference": reference}),
                            S.finish_refused("The confirmation was declined."))})
    if task_id == "d21":
        return woa({first: (S.finish_refused("当前账号没有权限创建客户，未执行任何写入。"),)})
    if task_id == "d22":
        existing = ctx.record("customer.duplicate_name")["name"]
        email = ctx.population["new_records"]["new.duplicate_email_name"]["email"]
        return woa({first: (S.load_skill_with("create_customer", writes=True),
                            S.call("create_customer", {"name": existing, "email": email}),
                            S.finish_refused("A customer with that name already exists."))})
    if task_id == "d24":
        name = ctx.record("customer.other_company_1")["name"]
        return woa({first: (S.load_skill_with("query_customers"),
                            S.call("query_customers", {"name_contains": name}),
                            S.finish_not_found())})
    if task_id == "d26":
        order = ctx.record("order.related")["name"]
        customer = ctx.record("customer.related_order")["name"]
        return woa({first: (S.load_skill_with("query_sale_orders"),
                            S.call("query_sale_orders", {"name_contains": order}),
                            S.load_skill_with("query_customers"),
                            S.call("query_customers", {"name_contains": customer}),
                            S.goto_route_of(customer), S.finish_acted(f"已打开客户 {customer} 的页面。"))})
    if task_id == "d30":
        return woa({first: (S.load_skill_with("query_sale_orders"),
                            S.call("query_sale_orders", {"date_from": "2026-03-01",
                                                         "date_until": "2026-04-01",
                                                         "limit": 50}),
                            S.compute("count"), S.goto_collection(),
                            S.finish_acted_with("aggregate", over="ComputationEvidence"))})
    raise KeyError(f"no WOA qualification program for {task_id}")


def a_program(ctx: Context, task_id: str, *, variant: str = "") -> Callable:
    if task_id == "d01":
        record = ctx.record("customer.lookup")
        email = (ctx.record("customer.lookup_page")["email"] if variant == "wrong_email"
                 else record["email"])
        text = f"{record['name']} 的邮箱是 {email}，电话是 {record['phone']}。"
        return S.PageAgentProgram((S.done_text(text),)).policy()
    if task_id == "d06":
        count = sum(1 for item in ctx.population["customers"]
                    if item["company"] == "main" and item["customer_rank"] > 0)
        return S.PageAgentProgram((S.done_text(f"当前一共有 {count} 个客户。"),)).policy()
    if task_id == "d03":
        return S.PageAgentProgram((
            S.click_text(r"title=Home Menu"), S.click_text(r">Invoicing"),
            S.click_text(r">Customers"), S.click_text(r">Invoices\b"),
            S.done_text("The customer invoices list is open."))).policy()
    raise KeyError(f"no PageAgent qualification program for {task_id}")


