"""Odoo 18 adapter: delegated JSON-RPC tools, two-sided identity, and bounded routes.

Odoo's identity story is not the one a browser-session hand-off usually gives you, and the
difference shapes this file:

  * the browser's ``session_id`` cookie is **HttpOnly** and is accepted only as a cookie
    (``odoo/http.py`` reads ``cookies.get('session_id')``; the old ``X-Openerp-Session-Id``
    header was removed), so the page can never hand its credential to a server-side tool;
  * the page can still *assert* who it is, without a secret, through
    ``/web/session/get_session_info``;
  * the server side acts through a **per-user API key** (``res.users.apikeys``), which the
    user generates in Odoo's own settings and which carries its own expiry.

That is exactly the two-sided shape ``bind_matching_identity`` wants: a page assertion and
a delegated assertion that must resolve to the same actor before any tool runs. Credentials
stay in ignored 0600 files; the audit record carries digests and counts, never a key, a
cookie, or a raw result row.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal
from importlib import resources
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from web_operation_agent.answering import StructuredQueryAnswer
from web_operation_agent.application import (APPLICATION_ADAPTER_VERSION,
                                             ApplicationDescriptor, ApplicationPorts,
                                             DeclaredDataClassifier,
                                             MutationReconciliation,
                                             ResultPostconditionVerifier,
                                             SourceCapability)
from web_operation_agent.business import (BusinessMutationIndeterminate,
                                          BusinessSelectionOption,
                                          BusinessMutationSpec, BusinessSkill,
                                          BusinessToolAuthenticationError,
                                          BusinessToolContractError, BusinessToolError,
                                          BusinessToolIdentityError,
                                          BusinessToolRequestError, BusinessToolResult,
                                          BusinessToolRuntime,
                                          BusinessToolUnavailableError,
                                          business_text_key, normalize_business_text)
from web_operation_agent.identity import (IdentityAssertion, OperationContext,
                                          PrincipalContext, PrincipalError,
                                          bind_matching_identity)
from web_operation_agent.mutation import MutationJournal

from .route import ODOO_ROUTE_PORT, ODOO_SOURCE_SCOPE
from .sourcemap import OdooCompiler, odoo_source_tree
from web_operation_agent.evidence import Origin, QueryEvidence, RecordEvidence


APPLICATION_ID = "odoo"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_STORAGE_STATE_BYTES = 2 * 1024 * 1024
_PAGE_SIZE = 200
_MAX_PAGES = 25
_EMAIL = re.compile(r"[^@\s,;<>]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                    r"[a-z]{2,24}\Z", re.I)
_SESSION_COOKIE = "session_id"
#: The read Skills a deployment always gets, and the write Skills it gets only when
#: mutations are explicitly enabled. Split by effect rather than listed and sliced by index:
#: the previous `_SKILLS[:3]` / `_SKILLS[3]` silently made the fourth entry the only write
#: Skill, which is a fact no reader could see and a fifth entry would have broken.
_READ_SKILLS = ("sale-orders-query.json", "customers-query.json", "products-query.json",
                "quotations-query.json", "customer-invoices-query.json")
_WRITE_SKILLS = ("customers-create.json", "sale-orders-confirm.json",
                 "sale-orders-invoice.json")


def _validated_origin(value: str) -> str:
    parsed = urlsplit(value if isinstance(value, str) else "")
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Odoo base_url must be an http(s) origin")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("plain HTTP Odoo origins must use loopback")
    default = 80 if parsed.scheme == "http" else 443
    port = parsed.port
    suffix = f":{port}" if port is not None and port != default else ""
    host = parsed.hostname.casefold()
    return f"{parsed.scheme}://{f'[{host}]' if ':' in host else host}{suffix}"


def _packaged_skill(filename: str) -> BusinessSkill:
    artifact = resources.files("web_operation_agent_odoo").joinpath("skills", filename)
    try:
        raw = artifact.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"packaged Odoo Skill is unavailable: {filename}") from exc
    return BusinessSkill.from_bytes(
        raw, artifact_uri=f"package://web_operation_agent_odoo/skills/{filename}")


# ---------------------------------------------------------------------------- collections

class _Collection:
    """One typed read: an Odoo model, the exact scope, and the page that shows it.

    ``domain`` is not a convenience filter. It is the adapter's promise that the tool's
    result set and the route it hands back describe the same records -- the Odoo action at
    ``action_xml_id`` applies the same restriction, whether as a stored domain or as the
    search filter its context switches on by default.

    ``route_scope_note`` is what happens when that promise cannot be kept. Odoo's Quotations
    action stores *no* domain at all: its page looks filtered only because its context sets
    ``search_default_my_quotation``, a default the user can clear. So the page and the tool
    genuinely describe different sets, and the honest move is to say which -- in the typed
    result, where the model and the product shell both see it, rather than in Skill prose
    that the next person will not know to keep true. A page agent cannot know this: the
    fact exists only in the action descriptor.
    """

    __slots__ = ("tool", "model", "domain", "fields", "scope", "action_xml_id",
                 "route", "answer_fields", "data_class", "route_scope_note")

    def __init__(self, *, tool: str, model: str, domain: list, fields: tuple[str, ...],
                 scope: str, action_xml_id: str, route: str,
                 answer_fields: tuple[str, ...], data_class: str,
                 route_scope_note: str = ""):
        self.tool = tool
        self.model = model
        self.domain = domain
        self.fields = fields
        self.scope = scope
        self.action_xml_id = action_xml_id
        self.route = route
        self.answer_fields = answer_fields
        self.data_class = data_class
        #: Empty when the page at ``route`` shows exactly this tool's scope.
        self.route_scope_note = route_scope_note


COLLECTIONS: tuple[_Collection, ...] = (
    _Collection(
        tool="query_sale_orders", model="sale.order",
        # `sale.action_orders` stores exactly this domain: the Orders page is confirmed
        # orders, and quotations are a different page.
        domain=[("state", "not in", ("draft", "sent", "cancel"))],
        fields=("name", "partner_id", "state", "amount_total", "currency_id",
                "date_order", "invoice_status"),
        scope="confirmed sales orders visible to the delegated user",
        action_xml_id="sale.action_orders", route="/odoo/orders",
        answer_fields=("reference", "customer", "state", "total"),
        data_class="business.sales_order"),
    _Collection(
        tool="query_customers", model="res.partner",
        domain=[("customer_rank", ">", 0)],
        fields=("name", "email", "phone", "mobile", "city", "country_id"),
        scope="customers visible to the delegated user",
        action_xml_id="account.res_partner_action_customer", route="/odoo/customers",
        answer_fields=("name", "email"),
        data_class="business.customer"),
    _Collection(
        tool="query_products", model="product.template",
        domain=[("sale_ok", "=", True)],
        fields=("name", "default_code", "list_price", "currency_id", "type"),
        scope="saleable products visible to the delegated user",
        action_xml_id="sale.product_template_action", route="/odoo/products",
        answer_fields=("name", "code", "price"),
        data_class="business.product"),
    _Collection(
        tool="query_quotations", model="sale.order",
        # The complement of `query_sale_orders`: what the Orders page excludes.
        domain=[("state", "in", ("draft", "sent"))],
        fields=("name", "partner_id", "state", "amount_total", "currency_id",
                "date_order", "validity_date"),
        scope="open quotations, neither confirmed nor cancelled",
        action_xml_id="sale.action_quotations_with_onboarding", route="/odoo/sales",
        answer_fields=("reference", "customer", "state", "total"),
        data_class="business.sales_order",
        route_scope_note=(
            "sale.action_quotations_with_onboarding stores no domain: the Quotations page "
            "lists every sales order and only defaults its search to "
            "search_default_my_quotation, which the user can clear. This tool's scope is "
            "narrower than that page's."),),
    _Collection(
        tool="query_customer_invoices", model="account.move",
        # `account.action_move_out_invoice` stores move_type in (out_invoice, out_refund);
        # this tool takes the invoice half, which is what a sales workflow asks about.
        domain=[("move_type", "=", "out_invoice")],
        fields=("name", "partner_id", "state", "amount_total", "currency_id",
                "amount_residual", "invoice_date", "invoice_date_due", "invoice_origin"),
        scope="customer invoices visible to the delegated user",
        action_xml_id="account.action_move_out_invoice",
        route="/odoo/customer-invoices",
        answer_fields=("reference", "customer", "state", "total"),
        data_class="business.invoice",
        route_scope_note=(
            "account.action_move_out_invoice also lists credit notes (out_refund); this "
            "tool reads invoices only."),),
)
_BY_TOOL = {collection.tool: collection for collection in COLLECTIONS}
CREATE_CUSTOMER_TOOL = "create_customer"
CONFIRM_QUOTATION_TOOL = "confirm_quotation"
INVOICE_SALE_ORDER_TOOL = "invoice_sale_order"
MUTATION_TOOLS = (CREATE_CUSTOMER_TOOL, CONFIRM_QUOTATION_TOOL, INVOICE_SALE_ORDER_TOOL)
_CUSTOMERS = _BY_TOOL["query_customers"]
_QUOTATIONS = _BY_TOOL["query_quotations"]
_ORDERS = _BY_TOOL["query_sale_orders"]
_INVOICES = _BY_TOOL["query_customer_invoices"]

#: `sale.advance.payment.inv.advance_payment_method` offers three values, and only one of
#: them is an ordinary invoice. This is the fact the roadmap singled out: the confirmed
#: sales order form carries two buttons both labelled "Create Invoice", separated only by
#: their `invisible` conditions, and the second silently proposes a percentage down
#: payment. The DOM renders one button with one label; the descriptor says which is which,
#: so the tool pins the value rather than letting a default decide.
REGULAR_INVOICE = "delivered"


def _query_schema(collection: _Collection, description: str) -> dict:
    filters = {}
    if "partner_id" in collection.fields:
        filters["customer_id"] = {
            "type": "integer", "minimum": 1,
            "description": "Exact res.partner id from a current query; not a display name."}
    date_field = "date_order" if collection.model == "sale.order" else "invoice_date"
    if date_field in collection.fields:
        for parameter, boundary in (("date_from", "inclusive"), ("date_until", "exclusive")):
            filters[parameter] = {
                "type": "string", "format": "date",
                "description": (f"Optional {boundary} YYYY-MM-DD bound on {date_field}. "
                                + ("Order datetimes use UTC midnight boundaries; date_order is "
                                   "creation time for quotations, confirmation time for orders."
                                   if date_field == "date_order" else
                                   "Invoice dates are calendar dates, not posting dates."))}
    return {
        "type": "function",
        "function": {
            "name": collection.tool,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "name_contains": {
                        "type": "string", "maxLength": 100,
                        "description": "Optional case-insensitive record name substring.",
                    },
                    "limit": {
                        "type": "integer", "minimum": 1, "maximum": 50,
                        "description": "Maximum rows to return; defaults to 20.",
                    },
                    **filters,
                },
                "additionalProperties": False,
            },
        },
    }


QUERY_SCHEMAS = {
    "query_sale_orders": _query_schema(
        _ORDERS,
        "Read confirmed Odoo sales orders visible to the currently delegated user."),
    "query_customers": _query_schema(
        _CUSTOMERS,
        "Read Odoo customers visible to the currently delegated user."),
    "query_products": _query_schema(
        _BY_TOOL["query_products"],
        "Read saleable Odoo products visible to the currently delegated user."),
    "query_quotations": _query_schema(
        _QUOTATIONS,
        "Read open Odoo quotations -- draft and sent sales orders -- visible to the "
        "currently delegated user."),
    "query_customer_invoices": _query_schema(
        _INVOICES,
        "Read Odoo customer invoices visible to the currently delegated user."),
}

CREATE_CUSTOMER_SCHEMA = {
    "type": "function",
    "function": {
        "name": CREATE_CUSTOMER_TOOL,
        "description": (
            "Propose creation of exactly one Odoo customer for explicit user confirmation. "
            "The server re-checks duplicates immediately before writing and returns the "
            "created record's route."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 200,
                         "description": "Required exact customer name supplied by the user."},
                "email": {"type": "string", "minLength": 3, "maxLength": 254,
                          "description": (
                              "Required customer email address supplied by the user; it "
                              "cannot be omitted or invented."
                          )},
            },
            "required": ["name", "email"],
            "additionalProperties": False,
        },
    },
}


def _reference_schema(tool: str, description: str, subject: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "reference": {
                        "type": "string", "minLength": 1, "maxLength": 64,
                        "description": subject,
                    },
                },
                "required": ["reference"],
                "additionalProperties": False,
            },
        },
    }


CONFIRM_QUOTATION_SCHEMA = _reference_schema(
    CONFIRM_QUOTATION_TOOL,
    "Propose confirming exactly one open Odoo quotation for explicit user confirmation. "
    "The server re-checks that the quotation is still open immediately before writing and "
    "reads the confirmed order back under the same user.",
    "Exact quotation reference, such as S00012.")

INVOICE_SALE_ORDER_SCHEMA = _reference_schema(
    INVOICE_SALE_ORDER_TOOL,
    "Propose creating one ordinary customer invoice for a confirmed Odoo sales order, for "
    "explicit user confirmation. Refused unless the order still has something to invoice. "
    "Never a down payment: the regular-invoice method is pinned.",
    "Exact sales order reference, such as S00007.")


# ---------------------------------------------------------------------------- credentials

class OdooApiKeyStore:
    """A per-user Odoo API key held in one ignored 0600 file.

    Odoo mints these in Settings -> My Profile -> Account Security -> New API Key, bound to
    one user and one expiry. Provisioning is therefore a deployment step performed by the
    person whose records the Agent will act on, not something this adapter can do for them.
    """

    def __init__(self, key_path: str | Path):
        self.path = Path(key_path)
        self._lock = threading.Lock()
        self._cached: dict | None = None

    def _read(self) -> dict:
        with self._lock:
            if self._cached is not None:
                return self._cached
            try:
                raw = self.path.read_bytes()
            except OSError as exc:
                raise BusinessToolAuthenticationError(
                    "the Odoo API key file is unreadable") from exc
            if len(raw) > 64 * 1024:
                raise BusinessToolAuthenticationError("the Odoo API key file is oversized")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BusinessToolAuthenticationError(
                    "the Odoo API key file is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise BusinessToolAuthenticationError("the Odoo API key file is malformed")
            values = {}
            for field, pattern in (("database", r"[A-Za-z0-9_.-]{1,63}"),
                                   ("login", r"[^\x00-\x1f\x7f]{1,254}"),
                                   ("api_key", r"[A-Za-z0-9_-]{16,256}")):
                value = payload.get(field)
                if not isinstance(value, str) or not re.fullmatch(pattern, value):
                    raise BusinessToolAuthenticationError(
                        f"the Odoo API key file has no valid {field}")
                values[field] = value
            self._cached = values
            return values

    @property
    def database(self) -> str:
        return self._read()["database"]

    @property
    def login(self) -> str:
        return self._read()["login"]

    @property
    def api_key(self) -> str:
        return self._read()["api_key"]


class OdooPageSessionIdentityResolver:
    """Resolve who the browser is logged in as, without ever exposing its credential.

    The session cookie is read from the browser context's storage state, used once against
    ``/web/session/get_session_info``, and never returned: the assertion this produces
    carries identifiers only.
    """

    def __init__(self, *, base_url: str, storage_state_path: str | Path | None = None,
                 storage_state: dict | None = None, timeout_s: float = 15):
        if (storage_state_path is None) == (storage_state is None):
            raise ValueError("supply exactly one of storage_state_path or storage_state")
        self.base_url = _validated_origin(base_url)
        self.storage_state_path = storage_state_path
        self._storage_state = storage_state
        self.timeout_s = timeout_s

    def _load(self) -> dict:
        if self._storage_state is not None:
            return self._storage_state
        path = Path(self.storage_state_path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise BusinessToolIdentityError(
                "the Odoo browser storage state is unreadable") from exc
        if len(raw) > _MAX_STORAGE_STATE_BYTES:
            raise BusinessToolIdentityError("the Odoo browser storage state is oversized")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BusinessToolIdentityError(
                "the Odoo browser storage state is not valid JSON") from exc

    def _session_cookie(self) -> str:
        state = self._load()
        cookies = state.get("cookies") if isinstance(state, dict) else None
        if not isinstance(cookies, list):
            raise BusinessToolIdentityError(
                "the Odoo browser storage state has no cookies")
        host = urlsplit(self.base_url).hostname or ""
        for cookie in cookies:
            if not isinstance(cookie, dict) or cookie.get("name") != _SESSION_COOKIE:
                continue
            domain = str(cookie.get("domain") or "").lstrip(".").casefold()
            if domain and domain != host.casefold():
                continue
            value = cookie.get("value")
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,512}", value):
                return value
        raise BusinessToolIdentityError(
            "the Odoo browser storage state carries no usable session cookie")

    def binding_digest(self) -> str:
        """A stable digest of the live session cookie, for detecting a replaced session."""
        import hashlib
        return hashlib.sha256(
            f"odoo:{self.base_url}:{self._session_cookie()}".encode("utf-8")).hexdigest()

    def resolve(self, *, lifetime_s: int = 300,
                now_ms: int | None = None) -> IdentityAssertion:
        cookie = self._session_cookie()
        request = Request(self.base_url + "/web/session/get_session_info",
                          data=json.dumps({"jsonrpc": "2.0", "method": "call",
                                           "params": {}}).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Cookie", f"{_SESSION_COOKIE}={cookie}")
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read(_MAX_RESPONSE_BYTES))
        except HTTPError as exc:
            raise BusinessToolIdentityError(
                f"Odoo rejected the page session ({exc.code})") from exc
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise BusinessToolIdentityError(
                "the Odoo page session could not be resolved") from exc
        info = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(info, dict) or info.get("uid") in (None, False):
            raise BusinessToolIdentityError(
                "the Odoo page session is no longer authenticated")
        companies = info.get("user_companies")
        company = (companies.get("current_company")
                   if isinstance(companies, dict) else None)
        database = info.get("db")
        uid = info.get("uid")
        if (not isinstance(database, str) or not database
                or isinstance(uid, bool) or not isinstance(uid, int)
                or isinstance(company, bool) or not isinstance(company, int)):
            raise BusinessToolIdentityError("the Odoo session info is incomplete")
        issued_at = int(time.time() * 1000) if now_ms is None else now_ms
        return IdentityAssertion(
            schema_version=1, source="page_session", application_id=APPLICATION_ID,
            tenant_id=database, workspace_id=str(company), actor_id=str(uid),
            issued_at=issued_at, expires_at=issued_at + lifetime_s * 1000)


# ------------------------------------------------------------------------------- executor

class OdooBusinessToolExecutor:
    """Typed reads and one guarded write over Odoo's delegated JSON-RPC endpoint."""

    def __init__(self, *, base_url: str, keys: OdooApiKeyStore, timeout_s: float = 20,
                 allow_mutations: bool = False):
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        self.base_url = _validated_origin(base_url)
        self.keys = keys
        self.timeout_s = timeout_s
        self.allow_mutations = allow_mutations
        self._uid: int | None = None
        self._lock = threading.Lock()

    # -- tool catalog -------------------------------------------------------------------
    def tool_schemas(self) -> tuple[dict, ...]:
        reads = tuple(QUERY_SCHEMAS[collection.tool] for collection in COLLECTIONS)
        writes = (CREATE_CUSTOMER_SCHEMA, CONFIRM_QUOTATION_SCHEMA,
                  INVOICE_SALE_ORDER_SCHEMA)
        return reads + (writes if self.allow_mutations else ())

    def tool_effects(self) -> dict[str, str]:
        effects = {collection.tool: "read" for collection in COLLECTIONS}
        if self.allow_mutations:
            effects.update({tool: "write" for tool in MUTATION_TOOLS})
        return effects

    # -- transport ----------------------------------------------------------------------
    def _rpc(self, params: dict) -> object:
        body = json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}
                          ).encode("utf-8")
        request = Request(self.base_url + "/jsonrpc", data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read(_MAX_RESPONSE_BYTES))
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise BusinessToolAuthenticationError(
                    "Odoo rejected the delegated API key") from exc
            raise BusinessToolUnavailableError(
                f"the Odoo API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BusinessToolUnavailableError("the Odoo API is unreachable") from exc
        except json.JSONDecodeError as exc:
            raise BusinessToolContractError("the Odoo API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise BusinessToolContractError("the Odoo API response is not an object")
        error = payload.get("error")
        if error is not None:
            name = ""
            if isinstance(error, dict) and isinstance(error.get("data"), dict):
                name = str(error["data"].get("name") or "")
            if "AccessDenied" in name or "AccessError" in name:
                raise BusinessToolAuthenticationError(
                    "Odoo denied the delegated user this operation")
            # The message may quote record content, so only the exception class crosses.
            raise BusinessToolError(f"Odoo refused the call ({name or 'unknown'})")
        return payload.get("result")

    def _authenticate(self) -> int:
        with self._lock:
            if self._uid is not None:
                return self._uid
        uid = self._rpc({"service": "common", "method": "authenticate",
                         "args": [self.keys.database, self.keys.login,
                                  self.keys.api_key, {}]})
        if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
            raise BusinessToolAuthenticationError(
                "the Odoo API key did not authenticate a user")
        with self._lock:
            self._uid = uid
        return uid

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        uid = self._authenticate()
        return self._rpc({"service": "object", "method": "execute_kw",
                          "args": [self.keys.database, uid, self.keys.api_key,
                                   model, method, args, kwargs or {}]})

    # -- identity -----------------------------------------------------------------------
    def resolve_delegated_identity(self, *, lifetime_s: int = 300,
                                   now_ms: int | None = None) -> IdentityAssertion:
        if isinstance(lifetime_s, bool) or not isinstance(lifetime_s, int) \
                or not 1 <= lifetime_s <= 3_600:
            raise ValueError("identity lifetime_s must be an integer from 1 to 3600")
        uid = self._authenticate()
        rows = self.call("res.users", "read", [[uid]], {"fields": ["login", "company_id"]})
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise BusinessToolIdentityError("Odoo returned no delegated user record")
        company = rows[0].get("company_id")
        company_id = company[0] if isinstance(company, list) and company else None
        if isinstance(company_id, bool) or not isinstance(company_id, int):
            raise BusinessToolIdentityError("the Odoo delegated user has no company")
        issued_at = int(time.time() * 1000) if now_ms is None else now_ms
        return IdentityAssertion(
            schema_version=1, source="delegated_api", application_id=APPLICATION_ID,
            tenant_id=self.keys.database, workspace_id=str(company_id),
            actor_id=str(uid), issued_at=issued_at,
            expires_at=issued_at + lifetime_s * 1000)

    @staticmethod
    def _identity_audit(context: OperationContext | None) -> dict:
        if context is None:
            return {"identity_binding": "unverified"}
        return {"identity_binding": "principal_bound",
                "identity": context.audit_projection()}

    # -- reads --------------------------------------------------------------------------
    @staticmethod
    def _read_args(name: str, args: dict) -> tuple[str, int, list]:
        unknown = set(args) - set(QUERY_SCHEMAS[name]["function"]["parameters"]["properties"])
        if unknown:
            raise BusinessToolRequestError(f"{name} received unknown arguments")
        needle = args.get("name_contains", "")
        if (not isinstance(needle, str) or len(needle) > 100
                or re.search(r"[\x00-\x1f\x7f]", needle)):
            raise BusinessToolRequestError(
                "name_contains must be a string of at most 100 characters")
        limit = args.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BusinessToolRequestError("limit must be an integer from 1 to 50")
        domain = []
        if "customer_id" in args:
            customer_id = args["customer_id"]
            if isinstance(customer_id, bool) or not isinstance(customer_id, int) or customer_id < 1:
                raise BusinessToolRequestError("customer_id must be a positive integer")
            domain.append(["partner_id", "=", customer_id])
        dates = {}
        for key, operator in (("date_from", ">="), ("date_until", "<")):
            if key not in args:
                continue
            value = args[key]
            try:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                dates[key] = date.fromisoformat(value)
            except ValueError:
                raise BusinessToolRequestError(f"{key} must be a valid YYYY-MM-DD date") from None
            field = "date_order" if _BY_TOOL[name].model == "sale.order" else "invoice_date"
            domain.append([field, operator, value + (" 00:00:00" if field == "date_order" else "")])
        if len(dates) == 2 and dates["date_from"] >= dates["date_until"]:
            raise BusinessToolRequestError("date_from must precede the exclusive date_until")
        return needle.strip(), limit, domain

    def _scan(self, collection: _Collection, extra_domain: list | None = None
              ) -> tuple[list[dict], int, dict]:
        """Read the whole scoped collection, and say plainly whether the scan finished.

        `search_count` under the same actor and the same domain is the completeness oracle:
        a truthful "I saw all of them" needs a number that did not come from the same page
        of results being counted.
        """
        domain = [list(clause) for clause in collection.domain] + (extra_domain or [])
        total = self.call(collection.model, "search_count", [domain])
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise BusinessToolContractError("Odoo returned an invalid record count")
        rows: list[dict] = []
        seen: set[int] = set()
        pages = 0
        while pages < _MAX_PAGES and len(rows) < total:
            page = self.call(collection.model, "search_read", [domain],
                             {"fields": list(collection.fields), "limit": _PAGE_SIZE,
                              "offset": len(rows), "order": "id"})
            if not isinstance(page, list):
                raise BusinessToolContractError("Odoo returned an invalid record page")
            pages += 1
            for row in page:
                record_id = row.get("id") if isinstance(row, dict) else None
                if (isinstance(record_id, bool) or not isinstance(record_id, int)
                        or record_id <= 0 or record_id in seen):
                    raise BusinessToolContractError("Odoo returned a missing or repeated record identity")
                seen.add(record_id)
                rows.append(row)
            if not page:
                break
        complete = len(rows) == total
        return rows, total, {
            "pages_scanned": pages,
            "records_scanned": len(rows),
            "complete": complete,
            "incomplete_reason": "" if complete else "page_budget_exhausted",
        }

    @staticmethod
    def _label(row: dict, field: str) -> str:
        value = row.get(field)
        if isinstance(value, list) and len(value) == 2:
            value = value[1]
        if value is False or value is None:
            return ""
        if isinstance(value, float):
            numeric = Decimal(str(value))
            if not numeric.is_finite():
                raise BusinessToolContractError("Odoo returned a non-finite number")
            return format(numeric, f".{max(2, -numeric.as_tuple().exponent)}f")
        return str(value)[:300]

    @staticmethod
    def _currency(row: dict) -> str:
        # sale.order uses its pricelist's currency, account.move its invoice currency,
        # and product.template exposes the catalog currency. A company's currency is
        # not a substitute for any of these record-level denominators.
        value = row.get("currency_id")
        if (not isinstance(value, list) or len(value) != 2
                or isinstance(value[0], bool) or not isinstance(value[0], int) or value[0] <= 0
                or not isinstance(value[1], str) or not value[1].strip()
                or len(value[1]) > 32 or re.search(r"[\x00-\x1f\x7f]", value[1])):
            raise BusinessToolContractError("Odoo returned money without its record currency")
        return value[1].strip()

    _NUMERIC_FIELDS = {"amount_total": "money", "list_price": "money", "amount_residual": "money"}
    _FIELD_MEANINGS = {
        "date_order": "order confirmation time / quotation creation time (UTC)",
        "invoice_date": "invoice calendar date", "invoice_date_due": "invoice due date",
        "validity_date": "quotation expiry date", "amount_total": "total including taxes",
        "amount_residual": "invoice amount due", "list_price": "catalog sales price",
    }

    def _evidence(self, collection: _Collection, pairs, *, name: str, observed_at: str,
                  matched: int, scanned, whole: bool, conditions: tuple[str, ...]) -> tuple:
        """Turn what this read returned into evidence an answer can cite.

        The projection beside it stays a display view. This is the same read expressed so a
        conclusion can stand on it: the record's own identity, the field's own name, whether
        the field was actually read, and -- for a number -- what it is denominated in.
        """
        reference = f"{name}#{len(pairs)}"
        origin = Origin(kind="typed", reference=reference, observed_at=observed_at,
                        scope=collection.scope + "; " + "; ".join(conditions))
        items = [QueryEvidence(
            evidence_id="query#1", origin=origin, collection=collection.tool.removeprefix("query_"),
            conditions=conditions,
            matched_count=matched, records_scanned=scanned, covers_whole_scope=whole)]
        index = 0
        for row, _item in pairs:
            record_id = row.get("id")
            if not isinstance(record_id, int):
                # Without the application's own identity there is nothing stable to cite,
                # and a row number would be an identity this invented.
                continue
            for field_name in collection.fields:
                value_type = self._NUMERIC_FIELDS.get(field_name, "text")
                raw = row.get(field_name)
                index += 1
                items.append(RecordEvidence(
                    evidence_id=f"record#{index}", origin=origin,
                    record_id=f"{collection.model}/{record_id}",
                    field_name=field_name,
                    field_meaning=self._FIELD_MEANINGS.get(field_name, field_name.replace("_", " ")),
                    field_was_read=field_name in row,
                    value=(self._label(row, field_name) if field_name in row
                           and raw is not False and raw is not None else None),
                    value_type=value_type if field_name in row else "",
                    unit=self._currency(row) if value_type == "money" and field_name in row else ""))
        return tuple(items)

    def _project(self, collection: _Collection, row: dict) -> dict:
        record_id = row.get("id")
        item = {"name": self._label(row, "name")}
        if collection.tool == "query_sale_orders":
            item = {"reference": self._label(row, "name"),
                    "customer": self._label(row, "partner_id"),
                    "state": self._label(row, "state"),
                    "total": self._label(row, "amount_total"),
                    "invoice_status": self._label(row, "invoice_status")}
        elif collection.tool == "query_quotations":
            item = {"reference": self._label(row, "name"),
                    "customer": self._label(row, "partner_id"),
                    "state": self._label(row, "state"),
                    "total": self._label(row, "amount_total"),
                    "valid_until": self._label(row, "validity_date")}
        elif collection.tool == "query_customer_invoices":
            # A draft `account.move` has no number; Odoo stores `False` there, and a row
            # that rendered it as "False" would be a fact the model could act on wrongly.
            item = {"reference": (self._label(row, "name")
                                  or "(draft, not yet numbered)"),
                    "customer": self._label(row, "partner_id"),
                    "state": self._label(row, "state"),
                    "total": self._label(row, "amount_total"),
                    "sale_order": self._label(row, "invoice_origin")}
        elif collection.tool == "query_customers":
            item = {"name": self._label(row, "name"), "email": self._label(row, "email"),
                    "phone": self._label(row, "phone"), "mobile": self._label(row, "mobile"),
                    "city": self._label(row, "city"),
                    "country": self._label(row, "country_id")}
        else:
            item = {"name": self._label(row, "name"),
                    "code": self._label(row, "default_code"),
                    "price": self._label(row, "list_price"),
                    "type": self._label(row, "type")}
        for field in ("currency_id", "date_order", "invoice_date", "invoice_date_due", "amount_residual"):
            if field in collection.fields:
                item[field] = self._label(row, field)
        if not isinstance(record_id, bool) and isinstance(record_id, int) and record_id > 0:
            item["ui_path"] = f"{collection.route}/{record_id}"
        return item

    @staticmethod
    def _selection_option(collection: _Collection, row: dict,
                          item: dict) -> BusinessSelectionOption | None:
        record_id = row.get("id")
        if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
            return None
        if collection.tool == "query_customers":
            fields = ("name", "email", "city")
        elif collection.tool == "query_products":
            fields = ("name", "code", "price")
        else:
            fields = ("reference", "customer", "total", "state")
        values = [str(item.get(field) or "").strip() for field in fields]
        values = [value for value in values if value]
        if not values:
            return None
        return BusinessSelectionOption(
            record_ref=f"{collection.model}/{record_id}",
            label=" — ".join(values)[:200],
        )

    @staticmethod
    def _match_field(collection: _Collection) -> str:
        return "name" if "reference" not in collection.answer_fields else "reference"

    def execute(self, name: str, args: dict, *,
                context: OperationContext | None = None) -> BusinessToolResult:
        collection = _BY_TOOL.get(name)
        if collection is None:
            raise BusinessToolRequestError("unknown Odoo business tool")
        needle, limit, extra_domain = self._read_args(name, args)
        rows, total, scan = self._scan(collection, extra_domain)
        conditions = tuple(f"{p[0]} {p[1]} {p[2]}" for p in [*collection.domain, *extra_domain])
        if needle:
            conditions += (f"name case-insensitive substring {needle!r}",)
        conditions = conditions or ("no filter",)
        page_matches = not collection.route_scope_note and not needle and not extra_domain
        # Kept paired with the row it came from: the projection is a display view -- labels
        # for people -- while evidence needs the record's own identity, and re-deriving one
        # from the other after filtering is how they drift apart.
        pairs = [(row, self._project(collection, row)) for row in rows]
        field = self._match_field(collection)
        folded = needle.casefold().strip()
        if folded:
            pairs = [pair for pair in pairs if folded in pair[1][field].casefold()]
        matched = len(pairs)
        visible_pairs = pairs[:limit]
        visible = [item for _row, item in visible_pairs]
        selection_options = tuple(
            option for row, item in visible_pairs
            if (option := self._selection_option(collection, row, item)) is not None)
        allowed_routes = (collection.route,) + tuple(
            item["ui_path"] for item in visible if isinstance(item.get("ui_path"), str))
        source_complete = scan["complete"]
        result_truncated = matched > limit or not source_complete
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        completeness = {
            "source_total_known": True,
            "source_complete": source_complete,
            "result_truncated": result_truncated,
            "pages_scanned": scan["pages_scanned"],
            "records_scanned": scan["records_scanned"],
            "incomplete_reason": scan["incomplete_reason"],
        }
        return BusinessToolResult(
            content={
                collection.tool.removeprefix("query_"): visible,
                "collection_ui_path": collection.route,
                # Whether that page shows this exact set. Only two of the five collections
                # can promise it, and the ones that cannot say so here rather than leaving
                # the model to assume the page and the answer agree.
                "collection_ui_path_shows_this_scope": page_matches,
                **({"collection_ui_path_note": collection.route_scope_note}
                   if collection.route_scope_note else {}),
                "matched_count": matched,
                "source_total": total,
                "scope": collection.scope,
                "conditions": list(conditions),
                "consistency": "bounded scan, not a database snapshot",
                "observed_at": observed_at,
                "completeness": completeness,
            },
            audit={
                "application": APPLICATION_ID,
                "operation": name,
                "model": collection.model,
                "matched_count": matched,
                "returned_count": len(visible),
                "source_total": total,
                "scope": collection.scope,
                "collection_ui_path_shows_this_scope": page_matches,
                "observed_at": observed_at,
                **completeness,
                **self._identity_audit(context),
            },
            allowed_routes=allowed_routes,
            evidence=self._evidence(collection, visible_pairs, name=name,
                                    observed_at=observed_at, matched=matched,
                                    scanned=scan["records_scanned"],
                                    whole=source_complete and not result_truncated,
                                    conditions=conditions),
            selection_options=selection_options,
            query_answer=StructuredQueryAnswer(
                collection=collection.tool.removeprefix("query_"),
                fields=collection.answer_fields,
                records=tuple(tuple(item.get(key, "") for key in collection.answer_fields)
                              for item in visible),
                source=f"Odoo delegated JSON-RPC {collection.model}",
                scope=collection.scope,
                observed_at=observed_at,
                source_complete=source_complete,
                result_truncated=result_truncated,
                matched_count=matched,
                source_total=total,
                records_scanned=scan["records_scanned"],
                incomplete_reason=scan["incomplete_reason"],
            ),
        )

    # -- the one write ------------------------------------------------------------------
    @staticmethod
    def _normalize_customer(args: dict) -> dict:
        if not isinstance(args, dict) or set(args) != {"name", "email"}:
            raise BusinessToolError("create_customer requires only name and email")
        name = normalize_business_text(args.get("name"), field="customer name", maximum=200)
        email = args.get("email")
        if not isinstance(email, str):
            raise BusinessToolError("customer email must be a string")
        email = unicodedata.normalize("NFKC", email).strip().casefold()
        if not _EMAIL.fullmatch(email):
            raise BusinessToolError("customer email must be a plain address")
        return {"name": name, "email": email}

    def _assert_no_duplicate(self, name: str, email: str) -> None:
        rows, total, scan = self._scan(_CUSTOMERS)
        if not scan["complete"] or len(rows) != total:
            raise BusinessToolError("the Odoo duplicate check was incomplete")
        for row in rows:
            existing_email = self._label(row, "email").casefold()
            if (business_text_key(self._label(row, "name")) == business_text_key(name)
                    or (existing_email and existing_email == email)):
                raise BusinessToolError(
                    "a customer with the same name or email already exists")

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
            raise BusinessToolError(
                f"sales order {reference} has invoice status {status or 'unknown'}; only "
                "an order with something still to invoice can be invoiced")
        return order

    def prepare_mutation(self, name: str, args: dict, *,
                         context: OperationContext | None = None) -> BusinessMutationSpec:
        if not self.allow_mutations or name not in MUTATION_TOOLS:
            raise BusinessToolError("unknown Odoo business mutation")
        if name == CONFIRM_QUOTATION_TOOL:
            reference = self._reference(name, args)
            quotation = self._one_order(reference, _QUOTATIONS, "quotation")
            return BusinessMutationSpec(
                args={"reference": reference},
                reconcile_key=reference.casefold(),
                effect="在 Odoo 中确认 1 张报价单",
                targets=(f"报价单：{reference}",
                         f"客户：{self._label(quotation, 'partner_id')}",
                         f"金额：{self._label(quotation, 'amount_total')}"),
            )
        if name == INVOICE_SALE_ORDER_TOOL:
            reference = self._reference(name, args)
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
        acknowledged = self.call("sale.order", "action_confirm", [[record_id]])
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
                              {"context": wizard_context})
        if isinstance(wizard_id, list) and wizard_id:
            wizard_id = wizard_id[0]
        if isinstance(wizard_id, bool) or not isinstance(wizard_id, int) or wizard_id <= 0:
            # Nothing has been written to the order yet: the wizard record is scratch state.
            raise BusinessToolContractError("Odoo returned an invalid invoicing wizard id")
        try:
            self.call("sale.advance.payment.inv", "create_invoices", [[wizard_id]],
                      {"context": wizard_context})
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
        rows = self.call("account.move", "read", [[invoice_id]],
                         {"fields": ["name", "state", "move_type", "amount_total",
                                     "partner_id", "invoice_origin"]})
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
        if not self.allow_mutations or name not in MUTATION_TOOLS:
            raise BusinessToolError("unknown Odoo business mutation")
        if name == CONFIRM_QUOTATION_TOOL:
            return self._confirm_quotation(self._reference(name, args), context)
        if name == INVOICE_SALE_ORDER_TOOL:
            return self._invoice_sale_order(self._reference(name, args), context)
        normalized = self._normalize_customer(args)
        # Repeat under the delegated actor immediately before dispatch. This closes the
        # ordinary time-of-check gap without ever retrying the create itself.
        self._assert_no_duplicate(normalized["name"], normalized["email"])
        created = self.call("res.partner", "create", [{
            "name": normalized["name"], "email": normalized["email"],
            "customer_rank": 1, "is_company": True,
        }])
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

class _OdooIdentityPort:
    required = True

    def __init__(self, executor: OdooBusinessToolExecutor,
                 required_scopes: frozenset[str]):
        self._executor = executor
        self.required_scopes = required_scopes

    def bind(self, page_identity: IdentityAssertion, *, delegation_id: str,
             lifetime_s: int = 300, now_ms: int | None = None) -> PrincipalContext:
        delegated = self._executor.resolve_delegated_identity(
            lifetime_s=lifetime_s, now_ms=now_ms)
        return bind_matching_identity(
            page_identity, delegated, delegation_id=delegation_id,
            scopes=tuple(sorted(self.required_scopes)), now_ms=now_ms)

    def validate(self, principal: PrincipalContext | None, *,
                 application_id: str) -> None:
        if principal is None:
            raise PrincipalError("Odoo requires a verified application principal")
        principal.require_live(application_id=application_id)


class _OdooMutationReconciler:
    """What the application can still say about a write whose outcome was lost.

    Never a verdict. Every one of these returns `unknown` with `requires_human`: a matching
    record is not proof that *this* proposal created it, and a missing one cannot rule out a
    write still in flight. What it does supply is the evidence an operator needs to decide,
    gathered under the same delegated actor, over a scan that says whether it finished.
    """

    #: Each write and the read scope its evidence needs.
    _SCOPES = {CREATE_CUSTOMER_TOOL: "customers:read",
               CONFIRM_QUOTATION_TOOL: "sale_orders:read",
               INVOICE_SALE_ORDER_TOOL: "sale_orders:read"}

    def __init__(self, executor: OdooBusinessToolExecutor, enabled: bool):
        self._executor = executor
        self.supported_tools = frozenset(self._SCOPES) if enabled else frozenset()

    def reconcile(self, name, business_key, *, evidence, principal):
        if name not in self.supported_tools or principal is None:
            raise ValueError("reconciliation requires an enabled tool and principal")
        principal.require_live(application_id=APPLICATION_ID)
        needed = self._SCOPES[name]
        if needed not in principal.scopes:
            raise PrincipalError(f"reconciliation requires {needed} scope")
        delegated = self._executor.resolve_delegated_identity()
        if any(getattr(principal, field) != getattr(delegated, field)
               for field in ("tenant_id", "workspace_id", "actor_id")):
            raise PrincipalError("reconciliation delegated identity changed")
        if name == CREATE_CUSTOMER_TOOL:
            reason = self._customer_evidence(business_key)
        else:
            reason = self._order_evidence(name, business_key)
        return MutationReconciliation(
            outcome="unknown", requires_human=True,
            reason=reason + " Establish the original writer's final outcome before "
                            "resolving.")

    def _customer_evidence(self, business_key) -> str:
        args = self._executor._normalize_customer(business_key)
        rows, total, scan = self._executor._scan(_CUSTOMERS)
        matches = [row for row in rows
                   if business_text_key(self._executor._label(row, "name"))
                   == business_text_key(args["name"])
                   and self._executor._label(row, "email").casefold() == args["email"]]
        return (f"Current matching records: {len(matches)}; complete scan: "
                f"{scan['complete'] and len(rows) == total}.")

    def _order_evidence(self, name: str, business_key) -> str:
        """For both order writes the evidence is the order's own current state.

        A confirm is visible as `state`, an invoice as the order's `invoice_ids`: both are
        facts the application owns, read now, rather than anything this process remembers.
        """
        reference = self._executor._reference(name, business_key)
        rows = self._executor.call(
            "sale.order", "search_read", [[["name", "=", reference]]],
            {"fields": ["name", "state", "invoice_status", "invoice_ids"], "limit": 5})
        if not isinstance(rows, list):
            raise BusinessToolContractError("Odoo returned an invalid reconciliation read")
        if len(rows) != 1:
            return (f"{len(rows)} sales orders currently carry the reference {reference}.")
        row = rows[0]
        invoices = row.get("invoice_ids")
        return (f"Sales order {reference} is currently in state "
                f"{self._executor._label(row, 'state')!r} with invoice status "
                f"{self._executor._label(row, 'invoice_status')!r} and "
                f"{len(invoices) if isinstance(invoices, list) else 0} linked invoices.")


class OdooApplicationAdapter:
    """Application-owned facts and tools for the Odoo 18 reference integration."""

    def __init__(self, *, base_url: str, api_key_path: str | Path,
                 allow_mutations: bool = False,
                 mutation_journal: MutationJournal | None = None,
                 page_controller_module: str | None = None,
                 timeout_s: float = 20):
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        if allow_mutations and mutation_journal is None:
            raise ValueError("allow_mutations=True requires an explicit mutation journal")
        self.origin = _validated_origin(base_url)
        self.page_controller_module = (
            page_controller_module or self.origin + "/woa/page-controller.js")
        executor = OdooBusinessToolExecutor(
            base_url=self.origin, keys=OdooApiKeyStore(api_key_path),
            timeout_s=timeout_s, allow_mutations=allow_mutations)
        skills = [_packaged_skill(name) for name in _READ_SKILLS]
        if allow_mutations:
            skills += [_packaged_skill(name) for name in _WRITE_SKILLS]
        required_scopes = {"query_sale_orders": "sale_orders:read",
                           "query_customers": "customers:read",
                           "query_products": "products:read",
                           "query_quotations": "sale_orders:read",
                           "query_customer_invoices": "invoices:read"}
        if allow_mutations:
            required_scopes[CREATE_CUSTOMER_TOOL] = "customers:write"
            required_scopes[CONFIRM_QUOTATION_TOOL] = "sale_orders:write"
            required_scopes[INVOICE_SALE_ORDER_TOOL] = "invoices:write"
        declarations = [
            (collection.tool,
             (collection.data_class, "business.identifier", "application.route"))
            for collection in COLLECTIONS]
        if allow_mutations:
            declarations += [
                (CREATE_CUSTOMER_TOOL,
                 ("business.customer", "business.identifier", "application.route",
                  "business.mutation_result")),
                (CONFIRM_QUOTATION_TOOL,
                 ("business.sales_order", "business.identifier", "application.route",
                  "business.mutation_result")),
                (INVOICE_SALE_ORDER_TOOL,
                 ("business.invoice", "business.identifier", "application.route",
                  "business.mutation_result")),
            ]
        data = DeclaredDataClassifier(tuple(sorted(declarations)))
        # Every write must be able to say the application confirmed it, under the same user.
        # A mutation without this pair in its result is refused by the core before it can be
        # reported as done.
        postconditions = ResultPostconditionVerifier(
            tuple((tool, ("content.readback_verified", "audit.readback_verified"))
                  for tool in MUTATION_TOOLS)
            if allow_mutations else ())
        routes = ODOO_ROUTE_PORT
        self.business_tools = BusinessToolRuntime(
            tuple(skills), executor, application_id=APPLICATION_ID,
            required_scopes=required_scopes, mutation_journal=mutation_journal,
            data_classifier=data, postcondition_verifier=postconditions,
            route_validator=routes)
        self._executor = executor
        self.ports = ApplicationPorts(
            identity=_OdooIdentityPort(executor, frozenset(required_scopes.values())),
            routes=routes,
            postconditions=postconditions,
            reconciliation=_OdooMutationReconciler(executor, allow_mutations),
            data=data,
        )
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id=APPLICATION_ID,
            display_name="Odoo 18",
            origin=self.origin,
            navigation_mode="spa",
            page_controller_module=self.page_controller_module,
            catalog_sha256=self.business_tools.catalog_sha256,
        )

    def source_capability(self, source_root: str | Path | None = None, *,
                          release: str = "",
                          attestation_id: str = "") -> SourceCapability:
        """Compile this deployment's Odoo source once and return what it enables.

        This is the adapter handing the composition a decision, not a deployment picking
        four flags by hand. With no source root -- the ordinary case, since source retrieval
        is optional -- the answer is "nothing", and the session runs on runtime facts alone.
        """
        if source_root is None:
            return SourceCapability.none()
        if not release:
            raise ValueError("an Odoo source root must be bound to an exact release")
        tree = odoo_source_tree(source_root, release=release)
        if not tree.available():
            raise ValueError("the Odoo source root does not exist")
        source_map = OdooCompiler(
            tree, attestation_id or f"odoo:{release}").compile()
        return SourceCapability(
            source_map=source_map,
            # Odoo's writes are button methods with no URL of their own, so the legacy
            # route-shaped operation contract has nothing to plan over here. What the map
            # does support is view-only candidates and cited runtime lookup.
            source_contract=False,
            runtime_source=True,
            source_affordance_mode="auto",
        )

    @property
    def source_scopes(self) -> frozenset[str]:
        """Odoo serves one web client, so every route lives in one scope."""
        return frozenset({ODOO_SOURCE_SCOPE})

    def health(self) -> dict:
        request = Request(self.origin + "/web/webclient/version_info",
                          data=json.dumps({"jsonrpc": "2.0", "method": "call",
                                           "params": {}}).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read(_MAX_RESPONSE_BYTES))
                info = payload.get("result") if isinstance(payload, dict) else None
                version = info.get("server_version") if isinstance(info, dict) else None
                return {"passed": response.status == 200 and isinstance(version, str),
                        "http_status": response.status,
                        "server_version": version if isinstance(version, str) else ""}
        except Exception:  # noqa: BLE001 — health is a boolean, never a leak
            return {"passed": False, "http_status": 0, "server_version": ""}

    def bind_principal(self, page_identity: IdentityAssertion, *, delegation_id: str,
                       lifetime_s: int = 300,
                       now_ms: int | None = None) -> PrincipalContext:
        """Match a trusted Odoo page-session assertion to the delegated API-key actor.

        The host must obtain ``page_identity`` from Odoo's own authenticated session. DOM
        text or a browser-supplied user id is not trusted evidence.
        """
        return self.ports.identity.bind(
            page_identity, delegation_id=delegation_id, lifetime_s=lifetime_s,
            now_ms=now_ms)

    def bind_browser_session(self, storage_state_path: str | Path, *, delegation_id: str,
                             lifetime_s: int = 300,
                             now_ms: int | None = None) -> PrincipalContext:
        """Bind the exact browser storage state to the delegated API-key identity."""
        page = OdooPageSessionIdentityResolver(
            base_url=self.origin, storage_state_path=storage_state_path,
            timeout_s=self._executor.timeout_s).resolve(
                lifetime_s=lifetime_s, now_ms=now_ms)
        return self.bind_principal(page, delegation_id=delegation_id,
                                   lifetime_s=lifetime_s, now_ms=now_ms)

    def page_session_binding(self, storage_state: str | Path | dict) -> str:
        """Hash the active Odoo session cookie without exposing it to the Agent."""
        source = ({"storage_state": storage_state} if isinstance(storage_state, dict)
                  else {"storage_state_path": storage_state})
        return OdooPageSessionIdentityResolver(
            base_url=self.origin, timeout_s=self._executor.timeout_s,
            **source).binding_digest()
