"""The Odoo adapter's ports, typed reads and single guarded write.

Every case here runs against a fake JSON-RPC transport. What it proves is the adapter's own
contract -- completeness accounting, route projection, the duplicate gate, the readback and
the two-sided identity -- not that a particular Odoo deployment is reachable. The live
qualification is a separate, opt-in gate.
"""
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.business import (BusinessToolAuthenticationError,
                                          BusinessToolDispatchError,
                                          BusinessToolPermissionError,
                                          BusinessToolUnavailableError)

from web_operation_agent.application import bind_application
from web_operation_agent.business import (BusinessMutationIndeterminate,
                                          BusinessMutationRefusedError,
                                          BusinessToolError, BusinessToolRequestError)
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.identity import IdentityAssertion, PrincipalError
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.mutation import MemoryMutationJournal
from web_operation_agent.schemas import ActionReceipt, PageObservation
from web_operation_agent.session import ServerAgentSession, TaskSpec
from web_operation_agent.tests.adapter_contract import assert_adapter_contract
from web_operation_agent.tests.chat_port_contract import CancellableChatDouble

from web_operation_agent_odoo.adapter import (APPLICATION_ID, COLLECTIONS,
                                              OdooApplicationAdapter,
                                              OdooBusinessToolExecutor,
                                              OdooPageSessionIdentityResolver,
                                              PREPARE_CUSTOMER_FORM_TOOL)

BASE = "http://127.0.0.1:8069"


class _RoutingChat(CancellableChatDouble):
    model = "routing-test"

    def complete(self, *_args, **_kwargs):
        raise AssertionError("constructing a routed session must not call the model")

    def complete_cancellable(self, *_args, **_kwargs):
        raise AssertionError("constructing a routed session must not call the model")


class _RoutingBrowser:
    def observe(self):
        raise AssertionError("constructing a routed session must not observe the page")

    def execute(self, *_args, **_kwargs):
        raise AssertionError("constructing a routed session must not use the browser")

    def console(self):
        return []


class _ProductRoutingChat(CancellableChatDouble):
    model = "routing-product-test"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def complete(self, _messages, _tools, _run_id, **_kwargs):
        self.calls += 1
        name, arguments = self.script.pop(0)
        return {"tool_calls": [{"id": f"call-{self.calls}", "function": {
            "name": name, "arguments": json.dumps(arguments),
        }}]}


class _ProductRoutingBrowser:
    def __init__(self):
        self.url = BASE + "/odoo/customers"
        self.epoch = 1
        self.actions = []

    def observe(self):
        return PageObservation(
            schema_version=1, epoch=self.epoch, url=self.url,
            title="Customers", landmarks=["Customers"], elements=[],
            content="Customers")

    def execute(self, action, args=None, expect_navigation=False):
        self.actions.append(action)
        before = self.url
        if action == "goto":
            self.url = BASE + str((args or {}).get("url") or "")
        self.epoch += 1
        return ActionReceipt(
            schema_version=1, command_id=f"command-{self.epoch}", action=action,
            args_echo=dict(args or {}), ok=True, url_before=before,
            url_after=self.url, navigated=self.url != before,
            epoch_before=self.epoch - 1, epoch_after=self.epoch,
            observation=self.observe())

    @staticmethod
    def console():
        return []


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
        elif operator == "ilike":
            literal = re.sub(r"\\([\\%_])", r"\1", str(value))
            matched = literal.casefold() in str(actual or "").casefold()
        else:
            raise AssertionError(f"the fake does not implement {operator!r}")
        if not matched:
            return False
    return True


class _FakeOdoo(OdooBusinessToolExecutor):
    """Answers `call` from an in-memory table; records every call it was asked to make."""

    def __init__(self, tables, *, allow_mutations=False, uid=7, tz=""):
        self.tables = tables
        self.allow_mutations = allow_mutations
        self.timeout_s = 5
        self.base_url = BASE
        self.calls = []
        self.mutation_calls = []
        self.created = []
        self._uid = uid
        # The real executor holds both; a double without them hid the zone lookup entirely.
        self._time_zone = None
        self._lock = threading.Lock()
        self.tz = tz
        self.keys = type("K", (), {"database": "woa", "login": "grower",
                                   "api_key": "0123456789abcdef0123"})()

    def _authenticate(self):
        return self._uid

    def call(self, model, method, args, kwargs=None, *, mutation=False):
        self.calls.append((model, method, args, dict(kwargs or {})))
        if (model == "res.users" and method == "read"
                and list((kwargs or {}).get("fields") or ()) == ["tz"]):
            return [{"id": self._uid, "tz": self.tz}]
        if mutation:
            self.mutation_calls.append((model, method))
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


class OdooRefusalsAreTypedByWhoRefused(unittest.TestCase):
    """`AccessDenied` rejects the credential; `AccessError` refuses an authenticated user."""

    def _refusal(self, exception_name: str):
        payload = json.dumps({"jsonrpc": "2.0", "id": None, "error": {
            "code": 200, "message": "Odoo Server Error",
            "data": {"name": exception_name, "message": "private record detail"}}}).encode()

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            @staticmethod
            def read(_limit=None):
                return payload

        executor = _FakeOdoo({})
        with patch("web_operation_agent_odoo.adapter.urlopen", return_value=_Response()):
            with self.assertRaises(BusinessToolError) as raised:
                OdooBusinessToolExecutor._rpc(executor, {"service": "object"})
        self.assertNotIn("private record detail", str(raised.exception))
        return raised.exception

    def test_an_access_rule_refusal_is_an_authorization_result(self):
        refused = self._refusal("odoo.exceptions.AccessError")
        self.assertIsInstance(refused, BusinessToolPermissionError)
        self.assertEqual(refused.code, "business_permission_denied")
        self.assertEqual(refused.layer, "application")

    def test_a_rejected_credential_is_still_an_authentication_fault(self):
        refused = self._refusal("odoo.exceptions.AccessDenied")
        self.assertIsInstance(refused, BusinessToolAuthenticationError)
        self.assertNotIsInstance(refused, BusinessToolPermissionError)

    def test_mutation_transport_loss_is_typed_as_an_unknown_dispatch(self):
        executor = _FakeOdoo({})
        with patch("web_operation_agent_odoo.adapter.urlopen",
                   side_effect=TimeoutError("lost response")):
            with self.assertRaises(BusinessToolDispatchError):
                OdooBusinessToolExecutor._rpc(
                    executor, {"service": "object"}, mutation=True)
            with self.assertRaises(BusinessToolUnavailableError) as read:
                OdooBusinessToolExecutor._rpc(executor, {"service": "object"})
        self.assertNotIsInstance(read.exception, BusinessToolDispatchError)


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
                         ["prepare_customer_form", "query_customer_invoices",
                          "query_customers", "query_products", "query_quotations",
                          "query_sale_orders"])
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

    def test_customer_page_context_narrows_an_entityless_open_without_granting_a_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)),
                allow_mutations=True, mutation_journal=MemoryMutationJournal())

        selection = adapter.business_tools.select(
            "打开 azur", allow_mutations=True,
            trusted_entry_path="/odoo/customers")

        self.assertTrue(selection.route_context_used)
        self.assertFalse(selection.fallback_used)
        self.assertEqual(selection.candidate_ids,
                         ("odoo.sales.customers-query",))
        self.assertTrue(all(skill.read_only for skill in selection.skills))

    def test_real_product_scope_cannot_route_an_open_request_to_the_form_draft(self):
        """Exercise the shipped catalog through the product session's actual boundary."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(root),
                allow_mutations=True, mutation_journal=MemoryMutationJournal())
            session = ServerAgentSession(
                task=TaskSpec(
                    task_ref="T", prompt="打开 azur", entry_path="/odoo/customers",
                    supported_scope=(
                        "Read current Odoo records through the typed API, and make at most "
                        "the one record change the user confirms, through a typed write tool. "
                        "Do not submit page forms. Open only an exact route returned by the API.")),
                chat=_RoutingChat(), browser=_RoutingBrowser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "session.jsonl", "real-odoo-routing"),
                business_tools=adapter.business_tools,
                business_mutation_approval=lambda *_args, **_kwargs: None,
                deterministic_read_navigation=True,
            )

        self.assertEqual(session._task_business_skill_ids,
                         {"odoo.sales.customers-query"})
        self.assertEqual(session._auto_loaded_business_skill_ids,
                         {"odoo.sales.customers-query"})
        self.assertNotIn("odoo.sales.customers-draft",
                         session._task_business_skill_ids)

    def test_real_product_opens_a_fuzzy_unique_customer_in_one_model_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(root),
                allow_mutations=True, mutation_journal=MemoryMutationJournal())
            adapter.business_tools._executor = _FakeOdoo({
                "res.partner": _customers([
                    ("Acme Corporation", "acme@example.test"),
                    ("Azure Interior", "azure@example.test"),
                ])})
            chat = _ProductRoutingChat([
                ("query_customers", {"name_contains": "azur"}),
            ])
            browser = _ProductRoutingBrowser()
            session = ServerAgentSession(
                task=TaskSpec(
                    task_ref="A02", prompt="打开 azur", entry_path="/odoo/customers",
                    supported_scope=(
                        "Read current Odoo records through the typed API. "
                        "Do not submit page forms.")),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=SessionLedger(root / "a02.jsonl", "a02"),
                business_tools=adapter.business_tools,
                business_mutation_approval=lambda *_args, **_kwargs: None,
                deterministic_read_navigation=True,
                application_routes=adapter.ports.routes,
            )
            session.entry_observation = browser.observe()
            session.plan()

            result = session.run()

        self.assertEqual(result.terminal_reason, "agent_finished")
        self.assertEqual(chat.calls, 1)
        self.assertEqual(browser.actions, ["goto"])
        self.assertEqual(browser.url, BASE + "/odoo/customers/11")

    def test_the_server_opened_record_is_confirmed_before_the_page_is_left(self):
        """The one-decision open path navigates on the server's own authority; it asks the
        person first like every other page switch, and does not leave without an answer."""
        for decision, reason, actions in (("approved", "agent_finished", ["goto"]),
                                          ("expired", "navigation_not_confirmed", [])):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                adapter = OdooApplicationAdapter(
                    base_url=BASE, api_key_path=_key_file(root),
                    allow_mutations=True, mutation_journal=MemoryMutationJournal())
                adapter.business_tools._executor = _FakeOdoo({
                    "res.partner": _customers([
                        ("Acme Corporation", "acme@example.test"),
                        ("Azure Interior", "azure@example.test"),
                    ])})
                asked = []

                def person(plan, *, context=None, decision=decision):
                    asked.append((plan.action, plan.url))
                    return type("Grant", (), {"decision": decision,
                                              "plan_digest": plan.content_digest})()

                browser = _ProductRoutingBrowser()
                session = ServerAgentSession(
                    task=TaskSpec(
                        task_ref="A02", prompt="打开 azur", entry_path="/odoo/customers",
                        supported_scope=(
                            "Read current Odoo records through the typed API. "
                            "Do not submit page forms.")),
                    chat=_ProductRoutingChat([
                        ("query_customers", {"name_contains": "azur"})]),
                    browser=browser, control=ReadOnlyControl(),
                    ledger=SessionLedger(root / "a02.jsonl", "a02"),
                    business_tools=adapter.business_tools,
                    business_mutation_approval=lambda *_args, **_kwargs: None,
                    navigation_confirmation=person,
                    deterministic_read_navigation=True,
                    application_routes=adapter.ports.routes,
                )
                session.entry_observation = browser.observe()
                session.plan()

                result = session.run()

                self.assertEqual(asked, [("goto", "/odoo/customers/11")])
                self.assertEqual(result.terminal_reason, reason)
                self.assertEqual(browser.actions, actions)

    def test_real_product_requeries_an_ambiguous_customer_after_the_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(root),
                allow_mutations=True, mutation_journal=MemoryMutationJournal())
            adapter.business_tools._executor = _FakeOdoo({
                "res.partner": _customers([
                    ("Acme Corporation", "acme@example.test"),
                    ("Azure Interior", "azure@example.test"),
                ])})
            first_chat = _ProductRoutingChat([
                ("query_customers", {"name_contains": "A"}),
            ])
            first_browser = _ProductRoutingBrowser()
            first = ServerAgentSession(
                task=TaskSpec(
                    task_ref="A03-first", prompt="打开 A",
                    entry_path="/odoo/customers",
                    supported_scope="Read current Odoo records. Do not submit page forms."),
                chat=first_chat, browser=first_browser, control=ReadOnlyControl(),
                ledger=SessionLedger(root / "a03-first.jsonl", "a03-first"),
                business_tools=adapter.business_tools,
                business_mutation_approval=lambda *_args, **_kwargs: None,
                deterministic_read_navigation=True, allow_clarification=True,
                application_routes=adapter.ports.routes,
            )
            first.entry_observation = first_browser.observe()
            first.plan()
            question = first.run()
            context = {
                "schema_version": 1,
                "prior_turns": [{
                    "message_id": "message-1", "user": "打开 A",
                    "assistant": question.agent_said, "status": "needs_input",
                }],
                "pending_clarification": question.clarification,
            }
            second_chat = _ProductRoutingChat([
                ("query_customers", {"name_contains": "Azure Interior"}),
            ])
            second_browser = _ProductRoutingBrowser()
            second = ServerAgentSession(
                task=TaskSpec(
                    task_ref="A03-second", prompt="Azure Interior",
                    entry_path="/odoo/customers",
                    supported_scope="Read current Odoo records. Do not submit page forms."),
                chat=second_chat, browser=second_browser, control=ReadOnlyControl(),
                ledger=SessionLedger(root / "a03-second.jsonl", "a03-second"),
                business_tools=adapter.business_tools,
                business_mutation_approval=lambda *_args, **_kwargs: None,
                deterministic_read_navigation=True, allow_clarification=True,
                conversation_context=context, application_routes=adapter.ports.routes,
            )
            second.entry_observation = second_browser.observe()
            second.plan()
            opened = second.run()

        self.assertEqual(question.terminal_reason, "needs_input")
        self.assertEqual(first_chat.calls, 1)
        self.assertEqual([choice["label"] for choice in question.clarification["choices"]], [
            "Acme Corporation — acme@example.test — Paris",
            "Azure Interior — azure@example.test — Paris",
        ])
        self.assertEqual(opened.terminal_reason, "agent_finished")
        self.assertEqual(second_chat.calls, 1)
        self.assertEqual(second_browser.actions, ["goto"])
        self.assertEqual(second_browser.url, BASE + "/odoo/customers/11")

    def test_customer_creation_selects_the_mutation_skill_without_route_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)),
                allow_mutations=True, mutation_journal=MemoryMutationJournal())

        selection = adapter.business_tools.select(
            "创建客户 P72 Acceptance Write，邮箱 p72-acceptance-write@example.test",
            allow_mutations=True, trusted_entry_path="/odoo/customers")

        self.assertFalse(selection.route_context_used)
        self.assertIn("odoo.sales.customers-create", selection.candidate_ids)

    def test_customer_creation_keeps_write_intent_with_a_malformed_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)),
                allow_mutations=True, mutation_journal=MemoryMutationJournal())

        selection = adapter.business_tools.select(
            "创建客户名称为abc,邮箱test@com", allow_mutations=True,
            trusted_entry_path="/odoo/customers")

        self.assertEqual(selection.candidate_ids,
                         ("odoo.sales.customers-create",))
        create_schema = next(
            schema for schema in adapter.business_tools.schemas_for(
                {"odoo.sales.customers-create"})
            if schema["function"]["name"] == "create_customer")
        self.assertEqual(
            create_schema["function"]["parameters"]["properties"]["email"]["format"],
            "email")
        with self.assertRaisesRegex(
                BusinessToolRequestError, "complete address such as name@example.com"):
            OdooBusinessToolExecutor._normalize_customer(
                {"name": "abc", "email": "test@com"})

    def test_customer_invoice_wording_selects_the_mutation_skill_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)),
                allow_mutations=True, mutation_journal=MemoryMutationJournal())

        selection = adapter.business_tools.select(
            "为销售订单 S00001 创建客户发票。", allow_mutations=True,
            trusted_entry_path="/odoo/sales")

        self.assertIn("odoo.sales.sale-orders-invoice", selection.trigger_matches)
        self.assertIn("odoo.sales.sale-orders-invoice", selection.candidate_ids)
        invoice = next(
            skill for skill in selection.skills
            if skill.skill_id == "odoo.sales.sale-orders-invoice")
        self.assertFalse(invoice.read_only)

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

    def test_a_deployment_can_separate_private_rpc_from_the_public_https_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url="http://odoo:8069",
                public_origin="https://erp.example.test",
                allow_private_http=True,
                api_key_path=_key_file(Path(tmp)))

        self.assertEqual(adapter.rpc_origin, "http://odoo:8069")
        self.assertEqual(adapter.descriptor.origin, "https://erp.example.test")
        self.assertEqual(
            adapter.descriptor.page_controller_module,
            "https://erp.example.test/woa/page-controller.js")

    def test_a_deployment_can_narrow_the_principal_to_an_application_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = OdooApplicationAdapter(
                base_url=BASE, api_key_path=_key_file(Path(tmp)))
            page = IdentityAssertion(
                schema_version=1, source="page_session", application_id="odoo",
                tenant_id="woa", workspace_id="2", actor_id="7",
                issued_at=1000, expires_at=31_000)
            delegated = IdentityAssertion(
                schema_version=1, source="delegated_api", application_id="odoo",
                tenant_id="woa", workspace_id="2", actor_id="7",
                issued_at=1000, expires_at=31_000)
            with patch.object(
                    adapter._executor, "resolve_delegated_identity",
                    return_value=delegated):
                principal = adapter.bind_principal(
                    page, delegation_id="trial", lifetime_s=30,
                    now_ms=1001, scopes=("customers:read",))

        self.assertEqual(principal.scopes, ("customers:read",))
        with self.assertRaisesRegex(PrincipalError, "subset"):
            adapter.bind_principal(
                page, delegation_id="trial", lifetime_s=30,
                now_ms=1001, scopes=("admin:all",))

    def test_private_rpc_http_requires_an_explicit_deployment_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "plain HTTP"):
                OdooApplicationAdapter(
                    base_url="http://odoo:8069",
                    public_origin="https://erp.example.test",
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

    def test_a_reference_search_asks_odoo_for_the_record_name(self):
        # Orders, quotations and invoices project their `name` as "reference". The search must
        # still go to Odoo's `name`: `sale.order.reference` is the payment reference, and
        # `account.move` has no `reference` field at all.
        invoices = [{"id": 40, "name": "INV/2026/00002", "partner_id": [3, "Acme"],
                     "state": "posted", "amount_total": 90.0, "currency_id": [1, "USD"],
                     "amount_residual": 90.0, "move_type": "out_invoice",
                     "invoice_origin": "S00002"}]
        cases = (("query_sale_orders", {"sale.order": _orders(3)}, "S00002", "sale_orders"),
                 ("query_quotations", {"sale.order": _quotations([("S00007", "draft"),
                                                                  ("S00008", "sent")])},
                  "S00008", "quotations"),
                 ("query_customer_invoices", {"account.move": invoices}, "INV/2026/00002",
                  "customer_invoices"))
        for tool, tables, reference, key in cases:
            with self.subTest(tool=tool):
                executor = _FakeOdoo(tables)
                result = executor.execute(tool, {"name_contains": reference})
                self.assertEqual(result.content["matched_count"], 1)
                self.assertEqual(result.content[key][0]["reference"], reference)
                self.assertEqual(result.content["record_route_resolution"], "exact")
                counted = [call for call in executor.calls if call[1] == "search_count"]
                self.assertIn(["name", "ilike", reference], counted[-1][2][0])

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
        self.assertEqual(result.allowed_routes, ("/odoo/customers",))
        self.assertEqual(result.navigation_target, "")

    def test_an_order_choice_is_identified_without_odoo_s_own_state_word(self):
        """A choice a Chinese reader sees carries no coded value (P7.5 CR-30).

        The options for orders read "S00001 — Azure Interior — 287.50 — sale", while the
        same state in an answer reads 已确认. The reference identifies the record on its
        own, so the label stops at what identifies it.
        """
        executor = _FakeOdoo({"sale.order": _orders(2)})
        result = executor.execute("query_sale_orders", {"limit": 10})

        labels = [item.label for item in result.selection_options]
        self.assertEqual(labels, ["S00001 — Acme — 101.00", "S00002 — Acme — 102.00"])
        for state in ("sale", "draft", "sent", "cancel"):
            for label in labels:
                self.assertNotIn(state, label)

    def test_a_fuzzy_single_match_can_open_the_only_current_record(self):
        executor = _FakeOdoo({"res.partner": _customers(
            [("Acme Corporation", "a@example.test")])})
        result = executor.execute("query_customers", {"name_contains": "Acme"})

        self.assertEqual(result.content["record_route_resolution"], "unique")
        self.assertEqual(result.navigation_target, "/odoo/customers/10")
        self.assertEqual(result.allowed_routes,
                         ("/odoo/customers", "/odoo/customers/10"))
        self.assertEqual(len(result.selection_options), 1)

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
                with self.assertRaisesRegex(
                        BusinessMutationRefusedError, "already exists") as raised:
                    executor.prepare_mutation("create_customer", args)
                self.assertEqual(raised.exception.reason, "duplicate")
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
        self.assertEqual(executor.mutation_calls, [("res.partner", "create")])

    def test_a_readback_that_does_not_match_is_indeterminate_not_success(self):
        class Drifting(_FakeOdoo):
            def call(self, model, method, args, kwargs=None, *, mutation=False):
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


class CustomerFormPreparation(unittest.TestCase):
    def test_it_issues_exact_transient_bindings_without_creating_a_record(self):
        executor = _FakeOdoo({"res.partner": []})
        result = executor.execute(
            PREPARE_CUSTOMER_FORM_TOOL,
            {"name": "  NovaForge Labs  ", "email": " OPS@NovaForge.Example "})

        self.assertEqual(result.allowed_routes, ("/odoo/customers/new",))
        self.assertEqual(result.form_draft.route, "/odoo/customers/new")
        self.assertEqual(
            [(item.field, item.value) for item in result.form_draft.fields],
            [("name", "NovaForge Labs"), ("email", "ops@novaforge.example")])
        self.assertEqual(result.form_draft.fields[0].control, "combobox")
        self.assertEqual(result.form_draft.persistence, "transient_until_submit")
        self.assertEqual(result.content["submission"], "user_only")
        self.assertEqual(executor.created, [])

    def test_it_reuses_the_complete_duplicate_gate_before_prefill(self):
        executor = _FakeOdoo({"res.partner": _customers(
            [("NovaForge Labs", "ops@novaforge.example")])})
        with self.assertRaisesRegex(BusinessToolError, "already exists"):
            executor.execute(
                PREPARE_CUSTOMER_FORM_TOOL,
                {"name": "novaforge labs", "email": "other@example.test"})
        self.assertEqual(executor.created, [])


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

        def call(self, model, method, args, kwargs=None, *, mutation=False):
            if mutation:
                self.mutation_calls.append((model, method))
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
        self.assertEqual(executor.mutation_calls, [("sale.order", "action_confirm")])

    def test_an_acknowledged_confirm_that_does_not_read_back_is_indeterminate(self):
        """`action_confirm` returned True and the record is not confirmed: nobody may retry."""
        executor = self._Lifecycle(quotations=_quotations([("S00002", "draft")]))
        executor.invoice_status = "no"
        original = executor.call

        def confirm_without_effect(model, method, args, kwargs=None, *, mutation=False):
            if model == "sale.order" and method == "action_confirm":
                executor.confirmed.extend(args[0])
                return True
            return original(model, method, args, kwargs, mutation=mutation)

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
        self.assertEqual(executor.mutation_calls, [
            ("sale.advance.payment.inv", "create"),
            ("sale.advance.payment.inv", "create_invoices"),
        ])

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

    def test_an_invoice_that_exists_but_cannot_be_read_back_is_indeterminate(self):
        """Once the order lists the new invoice, the write has landed. A plain tool error from
        the last read would be journalled as a failed write with no side effect, and the user
        told nothing was invoiced while a draft invoice sits on the order."""
        executor = self._Lifecycle(
            orders=_invoiceable_orders([("S00007", "to invoice")]))
        original = executor.call

        def unreadable_invoice(model, method, args, kwargs=None, *, mutation=False):
            if model == "account.move" and method == "read":
                raise BusinessToolUnavailableError("the Odoo API is unreachable")
            return original(model, method, args, kwargs, mutation=mutation)

        executor.call = unreadable_invoice
        with self.assertRaises(BusinessMutationIndeterminate) as raised:
            executor.execute_mutation("invoice_sale_order", {"reference": "S00007"})
        self.assertEqual(raised.exception.phase, "readback")
        self.assertEqual(raised.exception.record_id, "50")
        self.assertEqual(executor.invoiced, [50])

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
