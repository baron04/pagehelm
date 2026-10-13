"""Typed tool, collection, mutation, and Skill specifications for Odoo 18."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit



APPLICATION_ID = "odoo"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_STORAGE_STATE_BYTES = 2 * 1024 * 1024
_PAGE_SIZE = 200
_MAX_PAGES = 25
_EMAIL = re.compile(r"[^@\s,;<>]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                    r"[a-z]{2,24}\Z", re.I)
_SESSION_COOKIE = "session_id"
def _validated_origin(value: str, *, allow_private_http: bool = False) -> str:
    parsed = urlsplit(value if isinstance(value, str) else "")
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Odoo base_url must be an http(s) origin")
    if (parsed.scheme == "http"
            and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            and not allow_private_http):
        raise ValueError("plain HTTP Odoo origins must use loopback")
    default = 80 if parsed.scheme == "http" else 443
    port = parsed.port
    suffix = f":{port}" if port is not None and port != default else ""
    host = parsed.hostname.casefold()
    return f"{parsed.scheme}://{f'[{host}]' if ':' in host else host}{suffix}"


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
                 "route", "answer_fields", "data_class", "route_scope_note",
                 "projection_fields", "selection_fields", "match_field",
                 "empty_value_labels", "description")

    def __init__(self, *, tool: str, model: str, domain: list, fields: tuple[str, ...],
                 scope: str, action_xml_id: str, route: str,
                 answer_fields: tuple[str, ...], data_class: str,
                 projection_fields: tuple[tuple[str, str], ...],
                 selection_fields: tuple[str, ...], match_field: str,
                 description: str,
                 empty_value_labels: tuple[tuple[str, str], ...] = (),
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
        # These views were once a chain keyed by tool name in `_project`. Keeping them with
        # the tool descriptor makes adding a query a local specification change.
        self.projection_fields = projection_fields
        self.selection_fields = selection_fields
        self.match_field = match_field
        self.empty_value_labels = dict(empty_value_labels)
        self.description = description
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
        data_class="business.sales_order",
        projection_fields=(("reference", "name"), ("customer", "partner_id"),
                           ("state", "state"), ("total", "amount_total"),
                           ("invoice_status", "invoice_status"),
                           ("currency_id", "currency_id"), ("date_order", "date_order")),
        selection_fields=("reference", "customer", "total"), match_field="reference",
        description="Read confirmed Odoo sales orders visible to the currently delegated user."),
    _Collection(
        tool="query_customers", model="res.partner",
        domain=[("customer_rank", ">", 0)],
        # `vat` is the tax identification number. Without it here the only route to it was
        # the customer page, where Odoo renders it as an input -- and a page observation
        # carries labels, not input values, so the question could only ever be answered
        # "undetermined".
        fields=("name", "email", "phone", "mobile", "city", "country_id", "vat"),
        scope="customers visible to the delegated user",
        action_xml_id="account.res_partner_action_customer", route="/odoo/customers",
        answer_fields=("name", "email"),
        data_class="business.customer",
        projection_fields=(("name", "name"), ("email", "email"), ("phone", "phone"),
                           ("mobile", "mobile"), ("city", "city"),
                           ("country", "country_id")),
        selection_fields=("name", "email", "city"), match_field="name",
        description="Read Odoo customers visible to the currently delegated user."),
    _Collection(
        tool="query_products", model="product.template",
        domain=[("sale_ok", "=", True)],
        fields=("name", "default_code", "list_price", "currency_id", "type"),
        scope="saleable products visible to the delegated user",
        action_xml_id="sale.product_template_action", route="/odoo/products",
        answer_fields=("name", "code", "price"),
        data_class="business.product",
        projection_fields=(("name", "name"), ("code", "default_code"),
                           ("price", "list_price"), ("type", "type"),
                           ("currency_id", "currency_id")),
        selection_fields=("name", "code", "price"), match_field="name",
        description="Read saleable Odoo products visible to the currently delegated user."),
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
        projection_fields=(("reference", "name"), ("customer", "partner_id"),
                           ("state", "state"), ("total", "amount_total"),
                           ("valid_until", "validity_date"),
                           ("currency_id", "currency_id"), ("date_order", "date_order")),
        selection_fields=("reference", "customer", "total"), match_field="reference",
        description=("Read open Odoo quotations -- draft and sent sales orders -- visible to "
                     "the currently delegated user."),
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
        projection_fields=(("reference", "name"), ("customer", "partner_id"),
                           ("state", "state"), ("total", "amount_total"),
                           ("sale_order", "invoice_origin"),
                           ("currency_id", "currency_id"), ("invoice_date", "invoice_date"),
                           ("invoice_date_due", "invoice_date_due"),
                           ("amount_residual", "amount_residual")),
        selection_fields=("reference", "customer", "total"), match_field="reference",
        description="Read Odoo customer invoices visible to the currently delegated user.",
        empty_value_labels=(("reference", "(draft, not yet numbered)"),),
        route_scope_note=(
            "account.action_move_out_invoice also lists credit notes (out_refund); this "
            "tool reads invoices only."),),
)
_BY_TOOL = {collection.tool: collection for collection in COLLECTIONS}
#: What a Chinese answer calls the fields and scopes the read tools report. Keyed by the exact
#: field names and scope strings in the evidence; see `BusinessToolRuntime`'s answer labels.
ANSWER_LABELS_ZH = {
    "name": "名称", "email": "邮箱", "phone": "电话", "mobile": "手机", "city": "城市",
    "country_id": "国家", "vat": "税号", "partner_id": "客户", "state": "状态",
    "amount_total": "含税总额", "currency_id": "币种",
    "date_order": "订单确认时间／报价创建时间", "invoice_status": "开票状态",
    "validity_date": "报价有效期", "default_code": "内部编号", "list_price": "销售价格",
    "type": "产品类型", "amount_residual": "未付金额", "invoice_date": "开票日期",
    "invoice_date_due": "到期日", "invoice_origin": "来源单据",
    "confirmed sales orders visible to the delegated user": "当前账号可见的已确认销售订单",
    "customers visible to the delegated user": "当前账号可见的客户",
    "saleable products visible to the delegated user": "当前账号可见的可售产品",
    "open quotations, neither confirmed nor cancelled": "未确认、未取消的报价单",
    "customer invoices visible to the delegated user": "当前账号可见的客户发票",
    # Selection values, keyed by field and value. `state` is shared by orders and invoices,
    # so its words fit both.
    "state=draft": "草稿", "state=sent": "已发送", "state=sale": "已确认", "state=cancel": "已取消",
    # What a refusal calls each tool: "your account is not permitted to read sales orders"
    # rather than "to perform this operation".
    "query_sale_orders": "读取销售订单", "query_quotations": "读取报价单",
    "query_customers": "读取客户", "query_products": "读取产品",
    "query_customer_invoices": "读取客户发票", "create_customer": "创建客户",
    "archive_customer": "归档客户",
    "confirm_quotation": "确认报价单", "invoice_sale_order": "为销售订单开票",
    "prepare_customer_form": "预填客户表单",
    "state=posted": "已过账", "invoice_status=to invoice": "待开票",
    "invoice_status=invoiced": "已开票", "invoice_status=upselling": "可追加销售",
    "invoice_status=no": "无需开票", "type=consu": "货物", "type=service": "服务",
    "type=combo": "组合",
}
CREATE_CUSTOMER_TOOL = "create_customer"
ARCHIVE_CUSTOMER_TOOL = "archive_customer"
PREPARE_CUSTOMER_FORM_TOOL = "prepare_customer_form"
PREPARE_CUSTOMER_EDIT_TOOL = "prepare_customer_edit_form"
CONFIRM_QUOTATION_TOOL = "confirm_quotation"
INVOICE_SALE_ORDER_TOOL = "invoice_sale_order"
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

#: How much of one field value an answer shows before it says the rest was left out.
_LABEL_CHARS = 300


def _query_schema(collection: _Collection, description: str) -> dict:
    # The narrowest read this collection offers, and the only way to reach one of two records
    # that share a name: a name search can never single either of them out, so their routes
    # stay unauthorised and neither can be opened.
    filters = {"record_id": {
        "type": "integer", "minimum": 1,
        "description": (f"Exact {collection.model} id from a record this turn already read; "
                        "not a display name.")}}
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
                                + ("Day boundaries are the signed-in user's own time zone; "
                                   "date_order is creation time for quotations, confirmation "
                                   "time for orders."
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
    collection.tool: _query_schema(collection, collection.description)
    for collection in COLLECTIONS
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
                "email": {"type": "string", "format": "email",
                          "minLength": 3, "maxLength": 254,
                          "description": (
                              "Required complete customer email address supplied by the user, "
                              "such as name@example.com; it cannot be omitted or invented."
                          )},
            },
            "required": ["name", "email"],
            "additionalProperties": False,
        },
    },
}

ARCHIVE_CUSTOMER_SCHEMA = {
    "type": "function",
    "function": {
        "name": ARCHIVE_CUSTOMER_TOOL,
        "description": (
            "Propose archiving exactly one active Odoo customer for explicit user "
            "confirmation. Supply the exact record_id returned by query_customers. The "
            "server reads the customer again before writing and verifies it is inactive."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "integer", "minimum": 1,
                    "description": (
                        "Exact res.partner id returned by query_customers in this task; "
                        "never infer an id from a name."
                    ),
                },
            },
            "required": ["record_id"],
            "additionalProperties": False,
        },
    },
}

PREPARE_CUSTOMER_FORM_SCHEMA = {
    "type": "function",
    "function": {
        "name": PREPARE_CUSTOMER_FORM_TOOL,
        "description": (
            "Prepare an exact Odoo customer form draft without submitting or persisting it. "
            "After this returns, call prefill_form with no arguments; the server uses only "
            "the adapter-issued route, fields and values."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 200,
                         "description": "Required exact customer name supplied by the user."},
                "email": {"type": "string", "format": "email",
                          "minLength": 3, "maxLength": 254,
                          "description": (
                              "Required complete email supplied by the user, such as "
                              "name@example.com.")},
            },
            "required": ["name", "email"],
            "additionalProperties": False,
        },
    },
}


#: Which fields of an existing customer this deployment will stage on the record's own
#: form. The person saves it; the assistant only types. Anything outside this list is not
#: reachable through the assistant at all.
EDITABLE_CUSTOMER_FIELDS = {
    "name": ("客户名称", "combobox"),
    "phone": ("电话", "textbox"),
    "email": ("邮箱", "textbox"),
}

PREPARE_CUSTOMER_EDIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": PREPARE_CUSTOMER_EDIT_TOOL,
        "description": (
            "Prepare an edit to one existing Odoo customer on that customer's own form, "
            "without saving it. Supply the exact record_id from a customer this turn read "
            "and only the fields the user asked to change. The user confirms the change "
            "before anything is typed and saves it themselves; Odoo saves the form when "
            "the page is left. After this returns, call prefill_form with no arguments."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "integer", "minimum": 1,
                    "description": ("Exact res.partner id from a customer this turn read; "
                                    "not a display name.")},
                "name": {"type": "string", "minLength": 1, "maxLength": 200,
                         "description": "New exact customer name supplied by the user."},
                "phone": {"type": "string", "minLength": 1, "maxLength": 64,
                          "description": "New exact phone supplied by the user."},
                "email": {"type": "string", "format": "email",
                          "minLength": 3, "maxLength": 254,
                          "description": "New complete email supplied by the user."},
            },
            "required": ["record_id"],
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


@dataclass(frozen=True)
class _MutationToolSpec:
    """One Odoo write's complete server-side contract.

    The method names deliberately include the readback in ``dispatch_method``: a mutation
    is not complete when RPC acknowledges it, but only when its application-owned
    postcondition has been read back under the same delegated actor.
    """

    name: str
    schema: dict
    prepare_method: str
    dispatch_method: str
    write_scope: str
    reconciliation_scope: str
    data_types: tuple[str, ...]


MUTATION_SPECS = (
    _MutationToolSpec(
        CREATE_CUSTOMER_TOOL, CREATE_CUSTOMER_SCHEMA,
        "_prepare_create_customer", "_dispatch_create_customer",
        "customers:write", "customers:read",
        ("business.customer", "business.identifier", "application.route",
         "business.mutation_result")),
    _MutationToolSpec(
        ARCHIVE_CUSTOMER_TOOL, ARCHIVE_CUSTOMER_SCHEMA,
        "_prepare_archive_customer", "_dispatch_archive_customer",
        "customers:write", "customers:read",
        ("business.customer", "business.identifier", "application.route",
         "business.mutation_result")),
    _MutationToolSpec(
        CONFIRM_QUOTATION_TOOL, CONFIRM_QUOTATION_SCHEMA,
        "_prepare_confirm_quotation", "_dispatch_confirm_quotation",
        "sale_orders:write", "sale_orders:read",
        ("business.sales_order", "business.identifier", "application.route",
         "business.mutation_result")),
    _MutationToolSpec(
        INVOICE_SALE_ORDER_TOOL, INVOICE_SALE_ORDER_SCHEMA,
        "_prepare_invoice_sale_order", "_dispatch_invoice_sale_order",
        "invoices:write", "sale_orders:read",
        ("business.invoice", "business.identifier", "application.route",
         "business.mutation_result")),
)
_MUTATION_BY_TOOL = {spec.name: spec for spec in MUTATION_SPECS}
MUTATION_TOOLS = tuple(spec.name for spec in MUTATION_SPECS)


# ---------------------------------------------------------------------------- credentials
