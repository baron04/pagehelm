"""Mutation preparation, dispatch, and postcondition readback for Odoo."""
from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime, timezone

from web_operation_agent.business.runtime import (BusinessMutationIndeterminate,
                                                  BusinessMutationRefusedError,
                                                  BusinessMutationSpec,
                                                  BusinessToolContractError,
                                                  BusinessToolDispatchError,
                                                  BusinessToolError,
                                                  BusinessToolRequestError,
                                                  BusinessToolResult,
                                                  business_text_key,
                                                  normalize_business_text)
from web_operation_agent.state.identity import OperationContext

from .specs import (APPLICATION_ID, CONFIRM_QUOTATION_TOOL, CREATE_CUSTOMER_TOOL,
                    INVOICE_SALE_ORDER_TOOL, MUTATION_SPECS, REGULAR_INVOICE,
                    _CUSTOMERS, _EMAIL, _INVOICES, _MUTATION_BY_TOOL, _ORDERS,
                    _QUOTATIONS)


class OdooMutationTools:
    """Fail-closed mutations layered on the same typed read and RPC primitives."""

    @staticmethod
    def _normalize_email(value: object) -> str:
        if not isinstance(value, str):
            raise BusinessToolRequestError("customer email must be a string")
        email = unicodedata.normalize("NFKC", value).strip().casefold()
        if not _EMAIL.fullmatch(email):
            raise BusinessToolRequestError(
                "customer email must be a complete address such as name@example.com")
        return email

    @classmethod
    def _normalize_customer(cls, args: dict) -> dict:
        if not isinstance(args, dict) or set(args) != {"name", "email"}:
            raise BusinessToolRequestError("create_customer requires only name and email")
        name = normalize_business_text(args.get("name"), field="customer name", maximum=200)
        return {"name": name, "email": cls._normalize_email(args.get("email"))}

    def _assert_no_duplicate(self, name: str, email: str) -> None:
        rows, total, scan = self._scan(_CUSTOMERS)
        if not scan["complete"] or len(rows) != total:
            raise BusinessToolError("the Odoo duplicate check was incomplete")
        for row in rows:
            existing_email = self._label(row, "email").casefold()
            if (business_text_key(self._label(row, "name")) == business_text_key(name)
                    or (existing_email and existing_email == email)):
                raise BusinessMutationRefusedError(
                    "a customer with the same name or email already exists",
                    reason="duplicate")

    # -- the two order-lifecycle writes -------------------------------------------------
    @staticmethod
    def _reference(name: str, args: dict) -> str:
        if not isinstance(args, dict) or set(args) != {"reference"}:
            raise BusinessToolError(f"{name} requires only a reference")
        return normalize_business_text(args.get("reference"), field="reference", maximum=64)

    def _one_order(self, reference: str, collection: _Collection, what: str) -> dict:
        """Exactly one record in the tool's own scope, over a scan it knows finished.

        Not `search` with a name filter: a reference that matches two records, or a scan
        that stopped early, must be an explicit refusal rather than a quiet "first match".
        A write aimed at the wrong order is the failure this whole project is about.
        """
        rows, total, scan = self._scan(collection)
        if not scan["complete"] or len(rows) != total:
            raise BusinessToolError(f"the Odoo {what} scan was incomplete")
        key = business_text_key(reference)
        matches = [row for row in rows
                   if business_text_key(self._label(row, "name")) == key]
        if not matches:
            raise BusinessToolError(
                f"no {what} with reference {reference} is open to this user")
        if len(matches) > 1:
            raise BusinessToolError(
                f"{len(matches)} records share the reference {reference}")
        return matches[0]

    def _invoiceable(self, reference: str) -> dict:
        """A confirmed order the wizard would accept, or a refusal that says why not.

        `sale.advance.payment.inv` raises `UserError` on an order with nothing to invoice;
        `sale.order.invoice_status` is a stored computed field that says so beforehand.
        Refusing here rather than letting Odoo raise is the difference between a proposal
        the user can act on and an error after they approved one.
        """
        order = self._one_order(reference, _ORDERS, "sales order")
        status = self._label(order, "invoice_status")
        if status != "to invoice":
            # Typed, because the product has to be able to say which rule refused. As a bare
            # BusinessToolError this reached the user as "a business rule refused this
            # operation", and a turn that never learned the rule answered with whatever the
            # order query had returned instead.
            raise BusinessMutationRefusedError(
                f"sales order {reference} has invoice status {status or 'unknown'}; only "
                "an order with something still to invoice can be invoiced",
                reason="nothing_to_invoice", subject=self._label(order, "name") or reference)
        return order

    def prepare_mutation(self, name: str, args: dict, *,
                         context: OperationContext | None = None) -> BusinessMutationSpec:
        spec = _MUTATION_BY_TOOL.get(name)
        if not self.allow_mutations or spec is None:
            raise BusinessToolError("unknown Odoo business mutation")
        return getattr(self, spec.prepare_method)(args, context)

    def _prepare_confirm_quotation(self, args: dict,
                                   context: OperationContext | None) -> BusinessMutationSpec:
        reference = self._reference(CONFIRM_QUOTATION_TOOL, args)
        quotation = self._one_order(reference, _QUOTATIONS, "quotation")
        return BusinessMutationSpec(
            args={"reference": reference},
            reconcile_key=reference.casefold(),
            effect="在 Odoo 中确认 1 张报价单",
            targets=(f"报价单：{reference}",
                     f"客户：{self._label(quotation, 'partner_id')}",
                     f"金额：{self._label(quotation, 'amount_total')}"),
        )

    def _prepare_invoice_sale_order(self, args: dict,
                                    context: OperationContext | None) -> BusinessMutationSpec:
        reference = self._reference(INVOICE_SALE_ORDER_TOOL, args)
        order = self._invoiceable(reference)
        return BusinessMutationSpec(
            args={"reference": reference},
            reconcile_key=reference.casefold(),
            effect="在 Odoo 中为 1 张销售订单开具 1 张普通客户发票",
            targets=(f"销售订单：{reference}",
                     f"客户：{self._label(order, 'partner_id')}",
                     f"金额：{self._label(order, 'amount_total')}",
                     "开票方式：普通发票（非预付款）"),
        )

    def _prepare_create_customer(self, args: dict,
                                 context: OperationContext | None) -> BusinessMutationSpec:
        normalized = self._normalize_customer(args)
        self._assert_no_duplicate(normalized["name"], normalized["email"])
        return BusinessMutationSpec(
            args=normalized,
            reconcile_key=normalized["name"].casefold(),
            effect="在 Odoo 中创建 1 个客户",
            targets=(f"客户：{normalized['name']}", f"邮箱：{normalized['email']}"),
        )

    def _confirm_quotation(self, reference: str,
                           context: OperationContext | None) -> BusinessToolResult:
        # Re-checked under the delegated actor immediately before dispatch. This closes the
        # ordinary time-of-check gap without ever retrying the confirm itself.
        quotation = self._one_order(reference, _QUOTATIONS, "quotation")
        record_id = quotation.get("id")
        if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
            raise BusinessToolContractError("Odoo returned an invalid quotation id")
        acknowledged = self.call(
            "sale.order", "action_confirm", [[record_id]], mutation=True)
        if acknowledged is not True:
            raise BusinessMutationIndeterminate(
                "Odoo did not acknowledge the confirmation",
                phase="acknowledgement", record_id=str(record_id))
        try:
            rows = self.call("sale.order", "read", [[record_id]],
                             {"fields": ["name", "state", "partner_id", "amount_total",
                                         "invoice_status"]})
        except BusinessToolError as exc:
            raise BusinessMutationIndeterminate(
                "the Odoo quotation was confirmed but same-user readback failed",
                phase="readback", record_id=str(record_id)) from exc
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise BusinessMutationIndeterminate(
                "the Odoo quotation was confirmed but same-user readback was malformed",
                phase="readback", record_id=str(record_id))
        state = self._label(rows[0], "state")
        if self._label(rows[0], "name") != reference or state != "sale":
            # `action_confirm` returned True and the record is not confirmed. That is not a
            # failure the run may retry: the write may have landed and been undone, or the
            # read may be stale, and only an operator can tell which.
            raise BusinessMutationIndeterminate(
                f"the Odoo quotation acknowledged confirmation but reads back as {state!r}",
                phase="readback", record_id=str(record_id))
        ui_path = f"{_ORDERS.route}/{record_id}"
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "confirmed": True, "readback_verified": True,
                # Both authorised routes appear here: the core refuses a result that hands
                # back a route the model cannot see, and it is right to.
                "collection_ui_path": _ORDERS.route,
                "sale_order": {"id": record_id, "reference": reference, "state": state,
                               "customer": self._label(rows[0], "partner_id"),
                               "total": self._label(rows[0], "amount_total"),
                               "invoice_status": self._label(rows[0], "invoice_status"),
                               "ui_path": ui_path},
                "scope": _ORDERS.scope, "observed_at": observed_at,
            },
            audit={
                "application": APPLICATION_ID, "operation": CONFIRM_QUOTATION_TOOL,
                "model": "sale.order", "records_written": 1, "readback_verified": True,
                "scope": _ORDERS.scope, "observed_at": observed_at,
                **self._identity_audit(context),
            },
            allowed_routes=(ui_path, _ORDERS.route),
        )

    def _invoice_sale_order(self, reference: str,
                            context: OperationContext | None) -> BusinessToolResult:
        order = self._invoiceable(reference)
        record_id = order.get("id")
        if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
            raise BusinessToolContractError("Odoo returned an invalid sales order id")
        before = self._invoice_ids(record_id)
        # Odoo's invoice step is a wizard record plus one call on it, both carrying the
        # order in their context. `REGULAR_INVOICE` is pinned rather than defaulted: the
        # other two values of this field create down payments.
        wizard_context = {"active_model": "sale.order", "active_ids": [record_id],
                          "active_id": record_id}
        wizard_id = self.call("sale.advance.payment.inv", "create",
                              [{"advance_payment_method": REGULAR_INVOICE}],
                              {"context": wizard_context}, mutation=True)
        if isinstance(wizard_id, list) and wizard_id:
            wizard_id = wizard_id[0]
        if isinstance(wizard_id, bool) or not isinstance(wizard_id, int) or wizard_id <= 0:
            # Nothing has been written to the order yet: the wizard record is scratch state.
            raise BusinessToolContractError("Odoo returned an invalid invoicing wizard id")
        try:
            self.call("sale.advance.payment.inv", "create_invoices", [[wizard_id]],
                      {"context": wizard_context}, mutation=True)
        except BusinessToolError as exc:
            # The wizard may have created the invoice before failing, so this is uncertain
            # rather than failed, and it is never retried.
            raise BusinessMutationIndeterminate(
                "the Odoo invoicing wizard did not report an outcome",
                phase="dispatch", record_id=str(record_id)) from exc
        try:
            created = [invoice for invoice in self._invoice_ids(record_id)
                       if invoice not in before]
        except BusinessToolError as exc:
            raise BusinessMutationIndeterminate(
                "the Odoo invoice was requested but same-user readback failed",
                phase="readback", record_id=str(record_id)) from exc
        if len(created) != 1:
            raise BusinessMutationIndeterminate(
                f"the Odoo invoicing wizard left {len(created)} new invoices on the order",
                phase="readback", record_id=str(record_id))
        invoice_id = created[0]
        try:
            rows = self.call("account.move", "read", [[invoice_id]],
                             {"fields": ["name", "state", "move_type", "amount_total",
                                         "partner_id", "invoice_origin"]})
        except BusinessToolError as exc:
            raise BusinessMutationIndeterminate(
                "the Odoo invoice was created but same-user readback failed",
                phase="readback", record_id=str(invoice_id)) from exc
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise BusinessMutationIndeterminate(
                "the Odoo invoice was created but same-user readback was malformed",
                phase="readback", record_id=str(invoice_id))
        move_type = self._label(rows[0], "move_type")
        if move_type != "out_invoice":
            raise BusinessMutationIndeterminate(
                f"the Odoo invoicing wizard produced a {move_type!r}, not a customer invoice",
                phase="readback", record_id=str(invoice_id))
        ui_path = f"{_INVOICES.route}/{invoice_id}"
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "invoiced": True, "readback_verified": True,
                "invoice": {
                    "id": invoice_id,
                    # A draft `account.move` has no reference yet: Odoo assigns one when it
                    # is posted. Saying so beats reporting `False` as a name.
                    "reference": self._label(rows[0], "name") or "(draft, not yet numbered)",
                    "state": self._label(rows[0], "state"),
                    "move_type": move_type,
                    "customer": self._label(rows[0], "partner_id"),
                    "total": self._label(rows[0], "amount_total"),
                    "sale_order": reference,
                    "ui_path": ui_path,
                },
                "collection_ui_path": _INVOICES.route,
                "invoice_method": "regular invoice (not a down payment)",
                "scope": _INVOICES.scope, "observed_at": observed_at,
            },
            audit={
                "application": APPLICATION_ID, "operation": INVOICE_SALE_ORDER_TOOL,
                "model": "account.move", "records_created": 1, "readback_verified": True,
                "advance_payment_method": REGULAR_INVOICE,
                "scope": _INVOICES.scope, "observed_at": observed_at,
                **self._identity_audit(context),
            },
            allowed_routes=(ui_path, _INVOICES.route),
        )

    def _invoice_ids(self, order_id: int) -> list[int]:
        rows = self.call("sale.order", "read", [[order_id]], {"fields": ["invoice_ids"]})
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise BusinessToolContractError("Odoo returned an invalid sales order record")
        invoices = rows[0].get("invoice_ids")
        if not isinstance(invoices, list) or any(
                isinstance(item, bool) or not isinstance(item, int) for item in invoices):
            raise BusinessToolContractError("Odoo returned invalid invoice references")
        return list(invoices)

    def execute_mutation(self, name: str, args: dict, *,
                         context: OperationContext | None = None) -> BusinessToolResult:
        spec = _MUTATION_BY_TOOL.get(name)
        if not self.allow_mutations or spec is None:
            raise BusinessToolError("unknown Odoo business mutation")
        return getattr(self, spec.dispatch_method)(args, context)

    def _dispatch_confirm_quotation(self, args: dict,
                                    context: OperationContext | None) -> BusinessToolResult:
        return self._confirm_quotation(
            self._reference(CONFIRM_QUOTATION_TOOL, args), context)

    def _dispatch_invoice_sale_order(self, args: dict,
                                     context: OperationContext | None) -> BusinessToolResult:
        return self._invoice_sale_order(
            self._reference(INVOICE_SALE_ORDER_TOOL, args), context)

    def _dispatch_create_customer(self, args: dict,
                                  context: OperationContext | None) -> BusinessToolResult:
        normalized = self._normalize_customer(args)
        # Repeat under the delegated actor immediately before dispatch. This closes the
        # ordinary time-of-check gap without ever retrying the create itself.
        self._assert_no_duplicate(normalized["name"], normalized["email"])
        created = self.call("res.partner", "create", [{
            "name": normalized["name"], "email": normalized["email"],
            "customer_rank": 1, "is_company": True,
        }], mutation=True)
        record_id = created[0] if isinstance(created, list) and created else created
        if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
            raise BusinessMutationIndeterminate(
                "Odoo accepted the create but returned no record id",
                phase="acknowledgement")
        try:
            rows = self.call("res.partner", "read", [[record_id]],
                             {"fields": ["name", "email"]})
        except BusinessToolError as exc:
            raise BusinessMutationIndeterminate(
                "the Odoo customer was created but same-user readback failed",
                phase="readback", record_id=str(record_id)) from exc
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise BusinessMutationIndeterminate(
                "the Odoo customer was created but same-user readback was malformed",
                phase="readback", record_id=str(record_id))
        actual_name = self._label(rows[0], "name")
        actual_email = self._label(rows[0], "email").casefold()
        if actual_name != normalized["name"] or actual_email != normalized["email"]:
            raise BusinessMutationIndeterminate(
                "the Odoo customer was created but same-user readback did not match",
                phase="readback", record_id=str(record_id))
        ui_path = f"{_CUSTOMERS.route}/{record_id}"
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "created": True, "readback_verified": True,
                "customer": {"id": record_id, "name": actual_name,
                             "email": actual_email, "ui_path": ui_path},
                "scope": _CUSTOMERS.scope, "observed_at": observed_at,
            },
            audit={
                "application": APPLICATION_ID, "operation": CREATE_CUSTOMER_TOOL,
                "model": "res.partner", "records_created": 1,
                "readback_verified": True, "scope": _CUSTOMERS.scope,
                "observed_at": observed_at, **self._identity_audit(context),
            },
            allowed_routes=(ui_path,),
        )


# ---------------------------------------------------------------------------------- ports
