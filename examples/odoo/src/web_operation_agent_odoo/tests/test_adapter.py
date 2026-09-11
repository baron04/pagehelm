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
        if method == "search_count":
            return len(rows)
        if method == "search_read":
            offset = (kwargs or {}).get("offset", 0)
            limit = (kwargs or {}).get("limit", len(rows))
            return rows[offset:offset + limit]
        if method == "read":
            wanted = set(args[0])
            return [row for row in rows if row["id"] in wanted]
        if method == "create":
            record = {"id": 100 + len(self.created), **args[0]}
            self.created.append(record)
            rows.append(record)
            return record["id"]
        raise AssertionError(f"unexpected call {model}.{method}")


def _orders(count):
    return [{"id": index, "name": f"S{index:05d}", "partner_id": [3, "Acme"],
             "state": "sale", "amount_total": 100.0 + index,
             "invoice_status": "no"} for index in range(1, count + 1)]


def _customers(rows):
    return [{"id": index, "name": name, "email": email, "city": "Paris",
             "country_id": [1, "France"]}
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
                         ["query_customers", "query_products", "query_sale_orders"])
        self.assertEqual(bound.business_tools.mutation_tool_names, frozenset())

    def test_a_mutation_adapter_satisfies_the_shared_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)),
                allow_mutations=True, mutation_journal=MemoryMutationJournal())
            bound = assert_adapter_contract(self, adapter)
        self.assertEqual(bound.business_tools.mutation_tool_names, {"create_customer"})
        self.assertIn("customers:write", bound.ports.identity.required_scopes)

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
