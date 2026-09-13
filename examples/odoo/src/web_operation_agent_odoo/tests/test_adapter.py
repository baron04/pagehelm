"""The Odoo adapter's ports, typed reads and single guarded write.

Every case here runs against a fake JSON-RPC transport. What it proves is the adapter's own
contract -- completeness accounting, route projection, the duplicate gate, the readback and
the two-sided identity -- not that a particular Odoo deployment is reachable. The live
qualification is a separate, opt-in gate.
"""
import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.application import bind_application
from web_operation_agent.business import (BusinessMutationIndeterminate, BusinessToolError,
                                          BusinessToolRequestError)
from web_operation_agent.identity import IdentityAssertion, PrincipalError
from web_operation_agent.mutation import MemoryMutationJournal
from web_operation_agent.tests.adapter_contract import assert_adapter_contract

from web_operation_agent_odoo.adapter import (APPLICATION_ID, COLLECTIONS,
                                              OdooApplicationAdapter,
                                              OdooBusinessToolExecutor,
                                              OdooPageSessionIdentityResolver)

BASE = "http://127.0.0.1:8069"


def _key_file(root: Path) -> Path:
    path = root / "api-key.json"
    path.write_text(json.dumps(
        {"database": "woa", "login": "grower", "api_key": "0123456789abcdef0123"}))
    path.chmod(0o600)
    return path


def _matches(row: dict, domain: list) -> bool:
    """Enough of Odoo's domain language for the clauses the adapter actually declares.

    The fake used to ignore the domain entirely, which meant every collection's scope --
    the adapter's central promise -- was untested offline: `query_quotations` would happily
    return confirmed orders. Only the four operators the adapter uses are supported, and an
    unknown one is an assertion rather than a quiet pass.
    """
    for field, operator, value in domain:
        actual = row.get(field)
        if isinstance(actual, list) and len(actual) == 2:
            actual = actual[0]
        if operator == "=":
            matched = actual == value
        elif operator == "in":
            matched = actual in tuple(value)
        elif operator == "not in":
            matched = actual not in tuple(value)
        elif operator == ">":
            matched = isinstance(actual, (int, float)) and actual > value
        elif operator == ">=":
            matched = actual is not None and actual is not False and actual >= value
        elif operator == "<":
            matched = actual is not None and actual is not False and actual < value
        else:
            raise AssertionError(f"the fake does not implement {operator!r}")
        if not matched:
            return False
    return True


class _FakeOdoo(OdooBusinessToolExecutor):
    """Answers `call` from an in-memory table; records every call it was asked to make."""

    def __init__(self, tables, *, allow_mutations=False, uid=7):
        self.tables = tables
        self.allow_mutations = allow_mutations
        self.timeout_s = 5
        self.base_url = BASE
        self.calls = []
        self.created = []
        self._uid = uid
        self.keys = type("K", (), {"database": "woa", "login": "grower",
                                   "api_key": "0123456789abcdef0123"})()

    def _authenticate(self):
        return self._uid

    def call(self, model, method, args, kwargs=None):
        self.calls.append((model, method, args, dict(kwargs or {})))
        rows = self.tables.get(model, [])
        # `search*` takes a domain in args[0]; `read` takes a list of ids. Only the former
        # is scoped, and a clause is always a triple.
        domain = (args[0] if method.startswith("search") and args
                  and isinstance(args[0], list) else [])
        scoped = [row for row in rows if _matches(row, domain)]
        if method == "search_count":
            return len(scoped)
        if method == "search_read":
            offset = (kwargs or {}).get("offset", 0)
            limit = (kwargs or {}).get("limit", len(scoped))
            page = scoped[offset:offset + limit]
            return [self._fields(row, kwargs) for row in page]
        if method == "read":
            wanted = set(args[0])
            return [self._fields(row, kwargs) for row in rows if row["id"] in wanted]
        if method == "create":
            record = {"id": 100 + len(self.created), **args[0]}
            self.created.append(record)
            rows.append(record)
            return record["id"]
        raise AssertionError(f"unexpected call {model}.{method}")

    @staticmethod
    def _fields(row: dict, kwargs: dict | None) -> dict:
        """Project the requested fields, as Odoo does, defaulting an absent x2many to [].

        Odoo always returns a requested field; the fake returning nothing for one made
        `invoice_ids` look like a contract violation rather than an empty relation.
        """
        wanted = (kwargs or {}).get("fields")
        if not wanted:
            return dict(row)
        projected = {"id": row["id"]}
        for field in wanted:
            projected[field] = row.get(field, [] if field.endswith("_ids") else False)
        return projected


def _orders(count):
    return [{"id": index, "name": f"S{index:05d}", "partner_id": [3, "Acme"],
             "state": "sale", "amount_total": 100.0 + index,
             "currency_id": [1, "USD"], "date_order": "2026-09-01 08:00:00",
             "invoice_status": "no"} for index in range(1, count + 1)]


def _quotations(rows):
    """`rows` is (reference, state); a quotation is a sale.order in draft or sent."""
    return [{"id": index, "name": reference, "partner_id": [3, "Ready Mat"],
             "state": state, "amount_total": 500.0 + index, "validity_date": "2026-12-31"}
            | {"currency_id": [1, "USD"], "date_order": "2026-09-01 08:00:00"}
            for index, (reference, state) in enumerate(rows, start=2)]


def _invoiceable_orders(rows):
    """`rows` is (reference, invoice_status)."""
    return [{"id": index, "name": reference, "partner_id": [3, "Acme"], "state": "sale",
             "amount_total": 100.0 + index, "currency_id": [1, "USD"], "invoice_status": status}
            for index, (reference, status) in enumerate(rows, start=1)]


def _customers(rows):
    return [{"id": index, "name": name, "email": email, "city": "Paris",
             "country_id": [1, "France"], "customer_rank": 1}
            for index, (name, email) in enumerate(rows, start=10)]


class AdapterContract(unittest.TestCase):
    def test_a_read_only_adapter_satisfies_the_shared_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)))
            bound = assert_adapter_contract(self, adapter)
        self.assertEqual(bound.descriptor.application_id, APPLICATION_ID)
        self.assertEqual(bound.descriptor.navigation_mode, "spa")
        self.assertEqual(sorted(bound.business_tools.tool_names),
                         ["query_customer_invoices", "query_customers", "query_products",
                          "query_quotations", "query_sale_orders"])
        self.assertEqual(bound.business_tools.mutation_tool_names, frozenset())

    def test_a_mutation_adapter_satisfies_the_shared_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)),
                allow_mutations=True, mutation_journal=MemoryMutationJournal())
            bound = assert_adapter_contract(self, adapter)
        self.assertEqual(bound.business_tools.mutation_tool_names,
                         {"create_customer", "confirm_quotation", "invoice_sale_order"})
        for scope in ("customers:write", "sale_orders:write", "invoices:write"):
            self.assertIn(scope, bound.ports.identity.required_scopes)

    def test_mutations_require_an_explicit_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "explicit mutation journal"):
                OdooApplicationAdapter(base_url=BASE,
                                       api_key_path=_key_file(Path(tmp)),
                                       allow_mutations=True)

    def test_a_non_loopback_plain_http_origin_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                OdooApplicationAdapter(base_url="http://erp.example.test",
                                       api_key_path=_key_file(Path(tmp)))

    def test_every_collection_names_a_route_its_own_port_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)))
        for collection in COLLECTIONS:
            with self.subTest(tool=collection.tool):
                self.assertEqual(
                    adapter.ports.routes.validate_tool_route(collection.route),
                    collection.route)
                self.assertEqual(
                    adapter.ports.routes.validate_tool_route(f"{collection.route}/7"),
                    f"{collection.route}/7")


class TypedReads(unittest.TestCase):
    def test_a_complete_scan_reports_the_source_total_it_checked_against(self):
        executor = _FakeOdoo({"sale.order": _orders(5)})
        result = executor.execute("query_sale_orders", {"limit": 3})
        self.assertEqual(result.content["matched_count"], 5)
        self.assertEqual(result.content["source_total"], 5)
        self.assertEqual(len(result.content["sale_orders"]), 3)
        completeness = result.content["completeness"]
        self.assertTrue(completeness["source_complete"])
        self.assertTrue(completeness["result_truncated"])
        self.assertEqual(completeness["records_scanned"], 5)
        self.assertEqual(completeness["incomplete_reason"], "")
        # The count comes from the application's own search_count, not from the page of
        # rows that was read: otherwise "I saw all of them" would be self-certifying.
        self.assertIn(("sale.order", "search_count", [[["state", "not in",
                                                        ("draft", "sent", "cancel")]]], {}),
                      executor.calls)
        self.assertEqual(result.query_answer.source_total, 5)
        self.assertEqual(result.query_answer.matched_count, 5)
        self.assertTrue(result.query_answer.source_complete)

    def test_an_exhausted_page_budget_is_reported_as_incomplete(self):
        executor = _FakeOdoo({"res.partner": _customers(
            [(f"Customer {index}", f"c{index}@example.test") for index in range(9000)])})
        result = executor.execute("query_customers", {"limit": 5})
        completeness = result.content["completeness"]
        self.assertFalse(completeness["source_complete"])
        self.assertTrue(completeness["result_truncated"])
        self.assertEqual(completeness["incomplete_reason"], "page_budget_exhausted")
        self.assertFalse(result.query_answer.source_complete)

    def test_a_projection_authorises_only_the_routes_it_returned(self):
        executor = _FakeOdoo({"res.partner": _customers(
            [("Acme", "a@example.test"), ("Zephyr", "z@example.test")])})
        result = executor.execute("query_customers", {"name_contains": "acme"})
        self.assertEqual(result.content["matched_count"], 1)
        self.assertEqual(result.allowed_routes,
                         ("/odoo/customers", "/odoo/customers/10"))
        self.assertEqual(result.content["customers"][0]["ui_path"], "/odoo/customers/10")
        self.assertEqual(result.query_answer.records, (("Acme", "a@example.test"),))
        self.assertEqual(
            [(item.record_ref, item.label) for item in result.selection_options],
            [("res.partner/10", "Acme — a@example.test — Paris")],
        )

    def test_same_name_customers_have_application_grounded_choice_labels(self):
        executor = _FakeOdoo({"res.partner": _customers(
            [("Acme", "london@example.test"), ("Acme", "paris@example.test")])})
        result = executor.execute("query_customers", {"name_contains": "Acme"})

        self.assertEqual(result.content["matched_count"], 2)
        self.assertEqual(
            [(item.record_ref, item.label) for item in result.selection_options],
            [
                ("res.partner/10", "Acme — london@example.test — Paris"),
                ("res.partner/11", "Acme — paris@example.test — Paris"),
            ],
        )

    def test_an_unknown_tool_or_argument_is_refused_before_any_call(self):
        executor = _FakeOdoo({"sale.order": _orders(1)})
        for name, args in (("query_invoices", {}),
                           ("query_sale_orders", {"unexpected": 1}),
                           ("query_sale_orders", {"limit": 0}),
                           ("query_sale_orders", {"limit": 51}),
                           ("query_sale_orders", {"name_contains": "x" * 101})):
            with self.subTest(name=name, args=args):
                with self.assertRaises(BusinessToolRequestError):
                    executor.execute(name, args)
        self.assertEqual(executor.calls, [])


class TheOneWrite(unittest.TestCase):
    def _executor(self, existing=()):
        return _FakeOdoo({"res.partner": _customers(existing)}, allow_mutations=True)

    def test_a_proposal_normalises_and_states_its_exact_effect(self):
        executor = self._executor()
        spec = executor.prepare_mutation(
            "create_customer", {"name": "  NovaForge  Labs ",
                                "email": "  Ops@NovaForge.Example "})
        self.assertEqual(spec.args, {"name": "NovaForge Labs",
                                     "email": "ops@novaforge.example"})
        self.assertEqual(spec.targets,
                         ("客户：NovaForge Labs", "邮箱：ops@novaforge.example"))

    def test_a_duplicate_name_or_email_is_refused_before_dispatch(self):
        executor = self._executor([("NovaForge Labs", "ops@novaforge.example")])
        for args in ({"name": "novaforge labs", "email": "other@example.test"},
                     {"name": "Different", "email": "OPS@novaforge.example"}):
            with self.subTest(args=args):
                with self.assertRaisesRegex(BusinessToolError, "already exists"):
                    executor.prepare_mutation("create_customer", args)
        self.assertEqual(executor.created, [])

    def test_a_write_is_read_back_under_the_same_actor(self):
        executor = self._executor()
        result = executor.execute_mutation(
            "create_customer", {"name": "NovaForge Labs",
                                "email": "ops@novaforge.example"})
        self.assertEqual(len(executor.created), 1)
        self.assertTrue(result.content["readback_verified"])
        self.assertTrue(result.audit["readback_verified"])
        self.assertEqual(result.allowed_routes, ("/odoo/customers/100",))
        self.assertEqual(result.audit["identity_binding"], "unverified")
        reads = [call for call in executor.calls if call[1] == "read"]
        self.assertEqual(len(reads), 1)

    def test_a_readback_that_does_not_match_is_indeterminate_not_success(self):
        class Drifting(_FakeOdoo):
            def call(self, model, method, args, kwargs=None):
                result = super().call(model, method, args, kwargs)
                if method == "read":
                    return [{**result[0], "name": "Something Else"}]
                return result

        executor = Drifting({"res.partner": []}, allow_mutations=True)
        with self.assertRaises(BusinessMutationIndeterminate) as raised:
            executor.execute_mutation(
                "create_customer", {"name": "NovaForge Labs",
                                    "email": "ops@novaforge.example"})
        self.assertEqual(raised.exception.phase, "readback")
        self.assertEqual(raised.exception.record_id, "100")

    def test_a_read_only_adapter_has_no_write_path_at_all(self):
        executor = _FakeOdoo({"res.partner": []})
        with self.assertRaises(BusinessToolError):
            executor.prepare_mutation("create_customer",
                                      {"name": "X", "email": "x@example.test"})


class TheOrderLifecycleWrites(unittest.TestCase):
    """Confirming a quotation and invoicing an order: what is checked before, and after."""

    class _Lifecycle(_FakeOdoo):
        """Enough Odoo to drive both writes: `action_confirm`, and the invoicing wizard."""

        def __init__(self, *, orders=(), quotations=(), invoice_status="to invoice",
                     wizard_raises=None, confirm_returns=True, invoices_created=1):
            super().__init__({"sale.order": list(orders) + list(quotations)},
                             allow_mutations=True)
            self.invoice_status = invoice_status
            self.wizard_raises = wizard_raises
            self.confirm_returns = confirm_returns
            self.invoices_created = invoices_created
            self.confirmed: list[int] = []
            self.invoiced: list[int] = []
            self._next_invoice = 50

        def call(self, model, method, args, kwargs=None):
            if model == "sale.order" and method == "action_confirm":
                self.calls.append((model, method, args, dict(kwargs or {})))
                for row in self.tables["sale.order"]:
                    if row["id"] in args[0]:
                        row["state"] = "sale"
                        row["invoice_status"] = self.invoice_status
                self.confirmed.extend(args[0])
                return self.confirm_returns
            if model == "sale.advance.payment.inv":
                self.calls.append((model, method, args, dict(kwargs or {})))
                if method == "create":
                    return 1
                if self.wizard_raises is not None:
                    raise self.wizard_raises
                order_id = (kwargs or {}).get("context", {}).get("active_id")
                for row in self.tables["sale.order"]:
                    if row["id"] == order_id:
                        made = [self._next_invoice + step
                                for step in range(self.invoices_created)]
                        self._next_invoice += self.invoices_created
                        row["invoice_ids"] = list(row.get("invoice_ids", [])) + made
                        row["invoice_status"] = "invoiced"
                        self.invoiced.extend(made)
                        self.tables.setdefault("account.move", []).extend(
                            {"id": invoice, "name": False, "state": "draft",
                             "move_type": "out_invoice", "amount_total": 173.0,
                             "partner_id": [3, "Acme"], "invoice_origin": "S00007"}
                            for invoice in made)
                return True
            return super().call(model, method, args, kwargs)

    # -- confirm ------------------------------------------------------------------------
    def test_the_proposal_names_the_record_the_customer_and_the_amount(self):
        executor = self._Lifecycle(quotations=_quotations([("S00002", "draft")]))
        spec = executor.prepare_mutation("confirm_quotation", {"reference": "S00002"})
        self.assertEqual(spec.args, {"reference": "S00002"})
        self.assertEqual(spec.targets,
                         ("报价单：S00002", "客户：Ready Mat", "金额：502.00"))
        self.assertEqual(executor.confirmed, [], "a proposal writes nothing")

    def test_a_reference_outside_the_open_quotations_is_refused_before_dispatch(self):
        """An already-confirmed order is not a quotation, and must not be confirmed twice."""
        executor = self._Lifecycle(orders=_invoiceable_orders([("S00007", "no")]))
        with self.assertRaisesRegex(BusinessToolError, "no quotation with reference"):
            executor.prepare_mutation("confirm_quotation", {"reference": "S00007"})
        self.assertEqual(executor.confirmed, [])

    def test_two_records_sharing_a_reference_are_refused_rather_than_guessed(self):
        executor = self._Lifecycle(
            quotations=_quotations([("S00002", "draft"), ("S00002", "sent")]))
        with self.assertRaisesRegex(BusinessToolError, "share the reference"):
            executor.prepare_mutation("confirm_quotation", {"reference": "S00002"})

    def test_a_confirm_is_read_back_and_authorises_only_what_it_shows(self):
        executor = self._Lifecycle(quotations=_quotations([("S00002", "draft")]))
        result = executor.execute_mutation("confirm_quotation", {"reference": "S00002"})
        self.assertEqual(executor.confirmed, [2])
        self.assertTrue(result.content["readback_verified"])
        self.assertEqual(result.content["sale_order"]["state"], "sale")
        self.assertEqual(result.allowed_routes, ("/odoo/orders/2", "/odoo/orders"))
        for route in result.allowed_routes:
            self.assertIn(route, json.dumps(result.content),
                          "a route the model may open has to be one it can see")

    def test_an_acknowledged_confirm_that_does_not_read_back_is_indeterminate(self):
        """`action_confirm` returned True and the record is not confirmed: nobody may retry."""
        executor = self._Lifecycle(quotations=_quotations([("S00002", "draft")]))
        executor.invoice_status = "no"
        original = executor.call

        def confirm_without_effect(model, method, args, kwargs=None):
            if model == "sale.order" and method == "action_confirm":
                executor.confirmed.extend(args[0])
                return True
            return original(model, method, args, kwargs)

        executor.call = confirm_without_effect
        with self.assertRaises(BusinessMutationIndeterminate) as raised:
            executor.execute_mutation("confirm_quotation", {"reference": "S00002"})
        self.assertEqual(raised.exception.phase, "readback")

    # -- invoice ------------------------------------------------------------------------
    def test_an_order_with_nothing_to_invoice_is_refused_before_the_user_is_asked(self):
        """The wizard raises a UserError on this; by then a person has already approved."""
        executor = self._Lifecycle(orders=_invoiceable_orders([("S00007", "no")]))
        with self.assertRaisesRegex(BusinessToolError, "invoice status no"):
            executor.prepare_mutation("invoice_sale_order", {"reference": "S00007"})
        self.assertEqual(executor.invoiced, [])

    def test_the_invoice_is_always_the_ordinary_one_never_a_down_payment(self):
        """Odoo's form has two buttons labelled Create Invoice; one is a down payment."""
        executor = self._Lifecycle(
            orders=_invoiceable_orders([("S00007", "to invoice")]))
        result = executor.execute_mutation("invoice_sale_order", {"reference": "S00007"})
        wizard = [call for call in executor.calls
                  if call[0] == "sale.advance.payment.inv" and call[1] == "create"]
        self.assertEqual(len(wizard), 1)
        self.assertEqual(wizard[0][2][0], {"advance_payment_method": "delivered"})
        self.assertEqual(result.audit["advance_payment_method"], "delivered")
        self.assertIn("not a down payment", result.content["invoice_method"])

    def test_the_created_invoice_is_read_back_and_reported_as_an_unnumbered_draft(self):
        executor = self._Lifecycle(
            orders=_invoiceable_orders([("S00007", "to invoice")]))
        result = executor.execute_mutation("invoice_sale_order", {"reference": "S00007"})
        invoice = result.content["invoice"]
        self.assertTrue(result.content["readback_verified"])
        self.assertEqual(invoice["state"], "draft")
        self.assertEqual(invoice["move_type"], "out_invoice")
        self.assertEqual(invoice["reference"], "(draft, not yet numbered)",
                         "Odoo numbers an invoice on posting; False is not a reference")
        self.assertEqual(result.allowed_routes,
                         ("/odoo/customer-invoices/50", "/odoo/customer-invoices"))

    def test_a_wizard_that_does_not_report_is_indeterminate_and_never_retried(self):
        executor = self._Lifecycle(
            orders=_invoiceable_orders([("S00007", "to invoice")]),
            wizard_raises=BusinessToolError("Odoo refused the call (unknown)"))
        with self.assertRaises(BusinessMutationIndeterminate) as raised:
            executor.execute_mutation("invoice_sale_order", {"reference": "S00007"})
        self.assertEqual(raised.exception.phase, "dispatch")

    def test_more_than_one_new_invoice_is_indeterminate_not_a_success(self):
        """One approval buys one invoice. Two is a surprise, and surprises are not settled."""
        executor = self._Lifecycle(
            orders=_invoiceable_orders([("S00007", "to invoice")]), invoices_created=2)
        with self.assertRaises(BusinessMutationIndeterminate) as raised:
            executor.execute_mutation("invoice_sale_order", {"reference": "S00007"})
        self.assertIn("2 new invoices", str(raised.exception))

    def test_a_read_only_adapter_has_neither_order_write(self):
        executor = _FakeOdoo({"sale.order": _orders(1)})
        for tool in ("confirm_quotation", "invoice_sale_order"):
            with self.subTest(tool=tool):
                with self.assertRaises(BusinessToolError):
                    executor.prepare_mutation(tool, {"reference": "S00001"})


class WhatAPageCannotSay(unittest.TestCase):
    """The scope facts that exist only in a descriptor, carried in the typed result.

    Odoo's Quotations action stores no domain: its page lists every sales order and only
    defaults its search filter. A tool whose scope is narrower has to say so, or the model
    opens that page believing it shows the answer it just gave.
    """

    def test_a_collection_whose_page_shows_more_says_so_in_its_result(self):
        executor = _FakeOdoo({"sale.order": _quotations([("S00002", "draft")])})
        result = executor.execute("query_quotations", {})
        self.assertFalse(result.content["collection_ui_path_shows_this_scope"])
        self.assertIn("stores no domain", result.content["collection_ui_path_note"])
        self.assertFalse(result.audit["collection_ui_path_shows_this_scope"])

    def test_a_collection_whose_page_matches_says_that_too(self):
        executor = _FakeOdoo({"sale.order": _orders(2)})
        result = executor.execute("query_sale_orders", {})
        self.assertTrue(result.content["collection_ui_path_shows_this_scope"])
        self.assertNotIn("collection_ui_path_note", result.content)


class Identity(unittest.TestCase):
    def test_the_delegated_assertion_uses_database_company_and_uid(self):
        executor = _FakeOdoo({"res.users": [
            {"id": 7, "login": "grower", "company_id": [2, "My Company"]}]})
        assertion = executor.resolve_delegated_identity(lifetime_s=60, now_ms=1_000_000)
        self.assertEqual(assertion.source, "delegated_api")
        self.assertEqual(assertion.application_id, APPLICATION_ID)
        self.assertEqual((assertion.tenant_id, assertion.workspace_id, assertion.actor_id),
                         ("woa", "2", "7"))
        self.assertEqual(assertion.expires_at, 1_060_000)

    def test_a_page_assertion_and_a_delegated_assertion_must_be_one_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)))
        adapter._executor = _FakeOdoo({"res.users": [
            {"id": 7, "login": "grower", "company_id": [2, "My Company"]}]})
        adapter.ports.identity._executor = adapter._executor
        page = IdentityAssertion(
            schema_version=1, source="page_session", application_id=APPLICATION_ID,
            tenant_id="woa", workspace_id="2", actor_id="7",
            issued_at=1_000_000, expires_at=1_600_000)
        principal = adapter.bind_principal(page, delegation_id="d1", lifetime_s=60,
                                           now_ms=1_000_000)
        self.assertEqual(principal.actor_id, "7")
        other = IdentityAssertion(
            schema_version=1, source="page_session", application_id=APPLICATION_ID,
            tenant_id="woa", workspace_id="2", actor_id="9",
            issued_at=1_000_000, expires_at=1_600_000)
        with self.assertRaises(PrincipalError):
            adapter.bind_principal(other, delegation_id="d1", lifetime_s=60,
                                   now_ms=1_000_000)

    def test_a_storage_state_without_an_odoo_session_cookie_is_refused(self):
        resolver = OdooPageSessionIdentityResolver(
            base_url=BASE, storage_state={"cookies": [
                {"name": "other", "value": "x" * 32, "domain": "127.0.0.1"}]})
        with self.assertRaisesRegex(Exception, "no usable session cookie"):
            resolver.resolve()

    def test_the_binding_digest_changes_with_the_session_and_hides_it(self):
        first = OdooPageSessionIdentityResolver(
            base_url=BASE, storage_state={"cookies": [
                {"name": "session_id", "value": "a" * 40, "domain": "127.0.0.1"}]}
        ).binding_digest()
        second = OdooPageSessionIdentityResolver(
            base_url=BASE, storage_state={"cookies": [
                {"name": "session_id", "value": "b" * 40, "domain": "127.0.0.1"}]}
        ).binding_digest()
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotIn("a" * 40, first)


class AdapterDeliveredSourceConfiguration(unittest.TestCase):
    """The four source settings arrive as one adapter decision, never as four flags."""

    def _adapter(self, tmp):
        return OdooApplicationAdapter(base_url=BASE, api_key_path=_key_file(Path(tmp)))

    def test_without_a_source_root_a_session_runs_on_runtime_facts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            capability = self._adapter(tmp).source_capability()
        self.assertEqual(capability.session_kwargs(), {
            "source_map": None, "source_contract": False,
            "runtime_source": False, "source_affordance_mode": "off"})

    def test_a_source_root_must_be_bound_to_an_exact_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "exact release"):
                self._adapter(tmp).source_capability(tmp)
            with self.assertRaisesRegex(ValueError, "does not exist"):
                self._adapter(tmp).source_capability(
                    Path(tmp) / "absent", release="18.0-fixture")

    def test_a_compiled_root_enables_affordances_and_lookup_but_not_the_contract(self):
        from web_operation_agent_odoo.tests.test_sourcemap import FIXTURES
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            for rel, body in FIXTURES.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            capability = self._adapter(tmp).source_capability(
                root, release="18.0-fixture")
        self.assertTrue(capability.source_map.relations)
        self.assertEqual(capability.source_affordance_mode, "auto")
        self.assertTrue(capability.runtime_source)
        # Odoo's writes are button methods with no route of their own, so the route-shaped
        # operation contract has nothing to plan over. Saying so is the point of the value.
        self.assertFalse(capability.source_contract)

    def test_the_adapter_names_the_single_scope_its_routes_live_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._adapter(tmp).source_scopes, frozenset({"odoo"}))


if __name__ == "__main__":
    unittest.main()
