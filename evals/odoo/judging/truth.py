"""Actor-scoped truth construction for Odoo evaluation tasks."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Callable

from evals.odoo.fixture.rpc import OdooActorClient

CUSTOMERS = [["customer_rank", ">", 0]]
QUOTATIONS = [["state", "in", ["draft", "sent"]]]
ORDERS = [["state", "=", "sale"]]
INVOICES = [["move_type", "=", "out_invoice"]]
SCOPES = {"customers": CUSTOMERS, "quotations": QUOTATIONS, "orders": ORDERS,
          "invoices": INVOICES, "products": [["sale_ok", "=", True]], "all": []}

class TruthUnavailable(RuntimeError):
    """The fixture does not hold what the task specification requires."""


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _label(value: Any) -> Any:
    return value[1] if isinstance(value, list) and len(value) == 2 else value


class Context:
    def __init__(self, job: dict[str, Any]):
        self.job = job
        self.input = job["evaluator_input"]
        self.params = self.input["params"]
        #: The attempt, run and arm this job judges. Every observation it reads must name them.
        self.scope = job["scope"]
        self._clients: dict[str, OdooActorClient] = {}

    def client(self, actor: str | None = None) -> OdooActorClient:
        actor = actor or self.input["actor"]
        if actor not in self._clients:
            self._clients[actor] = OdooActorClient(
                base_url=self.job["rpc_url"], api_key_file=self.job["actor_key_files"][actor])
        return self._clients[actor]

    def domain(self, extra: list[Any] | None = None, *, scope: str | None = None,
               inherit_domain: bool = True) -> list[Any]:
        selected = SCOPES[scope or self.params.get("scope", "all")]
        task_domain = (self.params.get("domain") or []) if inherit_domain else []
        return [*selected, *task_domain, *(extra or [])]

    def one(self, model: str, field: str, value: Any, fields: list[str], *,
            scope: str | None = None, actor: str | None = None,
            inherit_domain: bool = True) -> dict[str, Any]:
        rows = self.client(actor).search_read(
            model, self.domain([[field, "=", value]], scope=scope,
                               inherit_domain=inherit_domain),
            sorted({*fields, "name"}), limit=3)
        if len(rows) != 1:
            raise TruthUnavailable(f"expected exactly one {model} matching the task")
        return rows[0]


# -- truth -----------------------------------------------------------------------------------

def _truth_record(ctx: Context) -> dict[str, Any]:
    p = ctx.params
    row = ctx.one(p["model"], p.get("match_field", "name"), p["match"],
                  list(p.get("fields") or []) + ["id"])
    return {"record": row}


def _truth_absent(ctx: Context) -> dict[str, Any]:
    p = ctx.params
    count = ctx.client().search_count(
        p["model"], ctx.domain([[p.get("match_field", "name"), "ilike", p["match"]]]))
    if count != 0:
        raise TruthUnavailable("the record a not-found task names is visible to the actor")
    truth = {"visible_count": 0}
    if p.get("other_actor"):
        other = ctx.client(p["other_actor"]).search_read(
            p["model"], [[p.get("match_field", "name"), "=", p["match"]]],
            sorted({"name", *(p.get("leak_fields") or [])}), limit=2)
        if len(other) != 1:
            raise TruthUnavailable("the other company does not hold the cross-company record")
        truth["other_company_record"] = other[0]
    return truth


def _truth_count(ctx: Context) -> dict[str, Any]:
    return {"count": ctx.client().search_count(ctx.params["model"], ctx.domain())}


def _truth_amounts(ctx: Context) -> dict[str, Any]:
    p = ctx.params
    rows = ctx.client().search_read(p["model"], ctx.domain(),
                                    ["name", "currency_id", p["amount_field"]])
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        sums[_label(row["currency_id"])] += _decimal(row[p["amount_field"]])
    if not rows:
        raise TruthUnavailable("the amount task matches no records")
    return {"sums": {key: str(value) for key, value in sorted(sums.items())},
            "rows": len(rows)}


def _truth_argmax(ctx: Context) -> dict[str, Any]:
    p = ctx.params
    rows = ctx.client().search_read(p["model"], ctx.domain(),
                                    ["name", "currency_id", p["field"], "id"])
    if p.get("currency"):
        rows = [row for row in rows if _label(row.get("currency_id")) == p["currency"]]
    if not rows:
        raise TruthUnavailable("the extreme-value task matches no records")
    top = max(_decimal(row[p["field"]]) for row in rows)
    winners = [row for row in rows if _decimal(row[p["field"]]) == top]
    if len(winners) != 1:
        raise TruthUnavailable("the extreme value is tied")
    return {"record": winners[0], "value": str(top)}


def _truth_candidates(ctx: Context) -> dict[str, Any]:
    p = ctx.params
    rows = ctx.client().search_read(
        p["model"], ctx.domain([[p.get("match_field", "name"), "ilike", p["prefix"]]]),
        sorted({"name", "id", *(p.get("fields") or [])}))
    names = sorted(row["name"] for row in rows)
    chosen = [row for row in rows if row["name"] == p.get("choice")] if p.get("choice") else []
    if len(rows) < 2 or (p.get("choice") and len(chosen) != 1):
        raise TruthUnavailable("the ambiguity task does not hold two candidates and one choice")
    return {"candidates": names, "chosen": chosen[0] if chosen else None,
            "candidate_ids": sorted(row["id"] for row in rows)}


def _truth_two_records(ctx: Context) -> dict[str, Any]:
    p = ctx.params
    fields = list(p.get("fields") or []) + ["id"]
    return {"first": ctx.one(p["model"], p.get("match_field", "name"), p["first"], fields),
            "second": ctx.one(p["model"], p.get("match_field", "name"), p["second"], fields)}


def _truth_related(ctx: Context) -> dict[str, Any]:
    p = ctx.params
    row = ctx.one(p["model"], p.get("match_field", "name"), p["match"],
                  [p["related_field"], "id"])
    related = row[p["related_field"]]
    if not isinstance(related, list) or not related:
        raise TruthUnavailable("the related record is empty")
    truth = {"record": row, "related_id": related[0], "related_name": related[1]}
    if p.get("related_fields"):
        # The related record lives in its own model; the parent task's scope (for example
        # confirmed orders) is not a filter that model even has.
        truth["related"] = ctx.one(p["related_model"], "id", related[0],
                                   list(p["related_fields"]),
                                   scope=p.get("related_scope", "all"), inherit_domain=False)
    if p.get("related_count_model"):
        truth["related_count"] = ctx.client().search_count(
            p["related_count_model"],
            [*SCOPES[p.get("related_count_scope", "all")],
             [p["related_count_field"], "=", related[0]]])
    return truth


def _truth_selection(ctx: Context) -> dict[str, Any]:
    p = ctx.params
    included = ctx.client().search_read(p["model"], ctx.domain(),
                                        sorted({"name", *(p.get("fields") or [])}))
    excluded = ctx.client().search_read(
        p["model"], [*SCOPES[p.get("exclusion_scope", "all")], *(p.get("exclusion_domain") or [])],
        ["name"])
    included_names = {row["name"] for row in included}
    if not included:
        raise TruthUnavailable("the selection task matches no records")
    return {"included": included,
            "excluded": sorted({row["name"] for row in excluded} - included_names)}


def _truth_write_target(ctx: Context) -> dict[str, Any]:
    p = ctx.params
    if ctx.input["shape"] == "create_customer":
        existing = ctx.client().search_count("res.partner", [["name", "=", p["name"]]])
        if existing:
            raise TruthUnavailable("the customer to create already exists")
        return {"partners_before": ctx.client().search_count("res.partner", CUSTOMERS)}
    order = ctx.one("sale.order", "name", p["reference"],
                    ["state", "invoice_status", "invoice_ids", "id"], scope="all")
    return {"order": order, "orders_before": ctx.client().search_count("sale.order", ORDERS),
            "invoices_before": ctx.client().search_count("account.move", INVOICES)}


TRUTH: dict[str, Callable[[Context], dict[str, Any]]] = {
    "field_values": _truth_record, "open_record": _truth_record, "page_fact": _truth_record,
    "open_collection": lambda ctx: {}, "not_found": _truth_absent,
    "cross_company_not_found": _truth_absent, "count": _truth_count,
    "date_range_count_open": _truth_count, "currency_sum": _truth_amounts,
    "currency_breakdown": _truth_amounts, "argmax_open_or_name": _truth_argmax,
    "clarify_then_open": _truth_candidates, "clarify_then_fields": _truth_candidates,
    "clarify_then_cancel": _truth_candidates, "correction_open": _truth_two_records,
    "correction_fields": _truth_two_records, "related_open": _truth_related,
    "followup_fields": _truth_related, "followup_count": _truth_related,
    "list_selection": _truth_selection, "create_customer": _truth_write_target,
    "confirm_quotation": _truth_write_target, "invoice_order": _truth_write_target,
    "approval_denied": lambda ctx: {}, "permission_refusal": lambda ctx: {},
    "business_refusal": lambda ctx: {}, "unsupported_refusal": lambda ctx: {},
}


