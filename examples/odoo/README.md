# web-operation-agent-odoo

Odoo 18 support for [`web-operation-agent`](https://github.com/): the `/odoo/<path>` route
grammar, a source compiler that reads Odoo's own module XML and model Python into a cited
`SourceMap`, typed business tools over the delegated JSON-RPC API, and the deployment
composition that serves one turn against a live Odoo.

This package contains no generic Agent code. It implements the core package's ports —
`ApplicationIdentityPort`, `ApplicationRoutePort`, `BusinessPostconditionVerifier`,
`ApplicationMutationReconciler`, `BusinessDataClassifier`, and the offline `SourceCompiler` —
against Odoo, and nothing else.

```python
from web_operation_agent_odoo import ODOO_ROUTE_PORT, OdooCompiler
from web_operation_agent_odoo.adapter import OdooApplicationAdapter
```

## Reading guide for integrators

Read this integration in dependency order instead of starting from the demo:

1. `adapter.py` assembles the application contract and keeps the core application-neutral.
2. `server/specs.py` defines the public tool schemas and scopes.
3. `server/reads.py` and `server/mutations.py` implement bounded reads, guarded writes, and
   application-owned verification.
4. `server/identity.py`, `route.py`, and `server/transport.py` bind the browser actor, allowed
   navigation, and Odoo JSON-RPC boundary.
5. `skills/` contains the auditable business instructions paired with those tools.
6. `turn.py` and `demo/` show composition; they contain no Odoo business rules of their own.
7. `deploy/reference/` is a disposable local fixture, `deploy/single-node/` is the operated
   single-node example, and `deploy/addons/` is the shared Odoo addon used by both.

Only `src/web_operation_agent_odoo/` is packaged. Deployment files and evaluation fixtures
remain checkout-owned so importing the adapter never implies Docker, demo passwords, or a
particular topology.

## Identity: two sides that must agree

Odoo's session story is not the browser-hand-off shape most integrations use, and the
difference is the reason this adapter is worth reading:

* the browser's `session_id` cookie is **HttpOnly** and Odoo 18 accepts it **only** as a
  cookie — `odoo/http.py` reads `cookies.get('session_id')`, and the old
  `X-Openerp-Session-Id` header no longer exists. Page JavaScript therefore cannot hand its
  credential to a server-side tool, and should not be able to;
* the page can still *assert* who it is, with no secret in the answer, through
  `/web/session/get_session_info`;
* the server side acts through a **per-user API key** (`res.users.apikeys`) that the user
  generates in Odoo's own settings and that carries its own expiry.

`bind_browser_session` resolves both and refuses to produce a principal unless they name the
same actor. The mapping is `db` → tenant, the current company → workspace, `uid` → actor.

The [single-node pilot deployment](deploy/single-node/README.md) replaces the development browser-profile handoff with an application-native path. The Odoo addon resolves the HttpOnly session on the application server and signs only the current database, company, actor, and one browser Runtime instance. The browser couriers this short-lived event to the same-origin Agent endpoint. The Agent verifies freshness, signature, and one-use nonce, then matches it to the actor's separately registered API key before issuing a browser capability. Logout signs a revoke event for every session issued to that Odoo actor. Playwright is not part of this path.

Provisioning the API key is a **deployment step performed by the person whose records the
Agent will act on**: Settings → My Profile → Account Security → New API Key. Store it, the
database name and the login in one ignored `0600` JSON file:

```json
{"database": "woa", "login": "you@example.com", "api_key": "…"}
```

## Routes

Odoo serves its whole back office from one server route and resolves the rest in the
browser. Three subpath shapes carry meaning:

| Shape | Means |
|---|---|
| `/odoo/<action-path>` | a collection, named by `ir.actions.act_window.path` |
| `/odoo/<action-path>/<id>` | one record inside that action |
| `/odoo/<model.name>/<id>` | one record addressed by its model |

The action path is a stored field, not a convention this package invented: the compiler's
output was compared against every one of them in a live database with no mismatch.

## Source

`OdooCompiler` reads menus, actions, views and models into `SourceRelation` triples, each
with a citation that `SourceTree.verify` can re-check against the exact bytes. What that
buys is the class of fact a page cannot show. On a confirmed sales order, two buttons are
both labelled "Create Invoice":

```
invisible="invoice_status != 'to invoice'"                      → a regular invoice
invisible="invoice_status != 'no' or state != 'sale'"           → a percentage down payment
context="{'default_advance_payment_method': 'percentage'}"
```

The DOM renders one button with one label. `addons/sale/views/sale_order_views.xml:266-271`
says which one it is.

Two limits are deliberate and are recorded rather than guessed:

* **inheritance is linked, not merged.** Every view-derived relation carries `root_view`, so
  the union of a view family's declared fields is computable, but an `inherit_id` patch that
  *removes* or *replaces* a node is not applied. Read `root_view` as "declared somewhere in
  this view family", never as "present in the rendered arch";
* **cross-module record patches are not merged into definitions.** A module may write
  `<record id="other_module.some_action">` to amend a record it does not own; such a record
  declares no `res_model` and is reported in `unsupported`, never as a definition.

Source retrieval stays optional. `OdooApplicationAdapter.source_capability()` with no source
root returns "nothing", and the session runs on runtime facts alone.

```python
capability = adapter.source_capability(source_root, release="18.0-20260908+…")
config = AgentTurnConfig(..., source=capability)
```

The capability goes into the turn as one value. Handing over a selection of its fields is how
a deployment came to enable source affordances while silently dropping the `runtime_source`
this adapter declares — so `lookup_source` and `open_source_route`, the cited retrieval this
`SourceMap` exists to serve, were never on the model's tool surface at all.

## Keeping Skills current

Two commands, and `verify` is the entry to both:

```bash
python -m web_operation_agent_odoo.demo.skills verify  --source-root … --release …
python -m web_operation_agent_odoo.demo.skills reissue --source-root … --release … \
    --accept provenance          # add --apply to write
```

`verify` resolves every `src:` citation in every packaged Skill against the compiled map.
`reissue` takes the ones it judged red, recompiles their compiled facts, and keeps what a
person wrote: `instructions`, `intent`, `tool_names` and `read_only` cross over untouched,
and an `api:` citation — which names a published interface no source map can confirm — is
carried verbatim rather than dropped or reinvented. It prints a field-by-field diff and
writes nothing without `--apply`.

`--accept` names which compiled fields to take. It defaults to all three a compiler can
derive, and `provenance` alone is the one that repairs citation rot without touching anything
a person edited: these Skills' titles are hand-written and their trigger lists are bilingual,
and neither the Chinese aliases nor the chosen phrasing appear in any Odoo descriptor.

`reissue` needs to know which declared request each Skill was compiled from, and that is
declared in `skill_sources.py` rather than recovered from the artifact. Recovering it would
mean reading the subject out of the citations that are exactly what went wrong. A subject
this release's map no longer contains fails loudly: when Odoo renames or removes an action, a
person decides what the Skill is now about.

What none of this fixes: when the workflow itself changes, the instructions are still written
by a person. No retrieval scheme removes that, because what needs reviewing is judgement
rather than fact.

## Tools

| Tool | Model | Scope | Route |
|---|---|---|---|
| `query_sale_orders` | `sale.order` | confirmed orders (the Orders page's own domain) | `/odoo/orders` |
| `query_customers` | `res.partner` | customer rank above zero | `/odoo/customers` |
| `query_products` | `product.template` | saleable products | `/odoo/products` |
| `query_quotations` | `sale.order` | draft and sent quotations | `/odoo/sales` |
| `query_customer_invoices` | `account.move` | customer invoices, excluding credit notes | `/odoo/customer-invoices` |
| `prepare_customer_form` | `res.partner` | exact customer form draft; transient until user submission | `/odoo/customers/new` |
| `create_customer` | `res.partner` | one customer, opt-in | `/odoo/customers/<id>` |
| `confirm_quotation` | `sale.order` | one quotation, opt-in | `/odoo/orders/<id>` |
| `invoice_sale_order` | `sale.advance.payment.inv` | one regular invoice workflow, opt-in | `/odoo/customer-invoices/<id>` |

`prepare_customer_form` requires an exact user-supplied name and email, checks the complete
customer scope for duplicates, and lets the server prefill those exact values. It neither
creates a record nor clicks Save. The draft works without `--allow-mutations`; direct model
`fill`, `select`, `click` and `submit` actions remain disabled. This contract is valid because
this Odoo form does not persist the fields before Save. An integration with autosave must use
the confirmed mutation path instead.

Each read scans its whole scope and checks the row count against the application's own
`search_count`, so "I saw all of them" is answered by a number that did not come from the
page of rows being counted. `create_customer` requires a user-supplied name and email and is disabled unless a deployment passes
`allow_mutations=True` **and** a mutation journal; it normalises the proposal, re-checks
duplicates over a complete scan immediately before dispatch, and reads the record back under
the same actor.

Read evidence includes customer phone/mobile/email, each monetary record's own `currency_id`, quotation/order `date_order`, and invoice dates plus `amount_residual` (amount still due). Monetary values keep their supplied precision; a missing currency is a contract error, and the core refuses to sum or rank different currencies together. Filter by currency first to compute within one denomination. `amount_total` is the tax-inclusive total; product `list_price` is a catalog price, not an outstanding balance.

Order and invoice tools accept `customer_id` from a current `res.partner` read. They also accept `date_from` (inclusive) and `date_until` (exclusive), both `YYYY-MM-DD`: orders use UTC midnight bounds on `date_order`, which is confirmation time for confirmed orders and creation time for quotations; invoices use calendar `invoice_date`, not posting date. Customers/products reject those inapplicable filters. These parameters narrow the tool's declared scope; they do not enable cancelled-order or arbitrary-domain queries. Relative dates require a known current time and an explicit interpretation.

Every read reports the executed conditions. Relation, date, customer-ID and `name_contains` filters are pushed into the Odoo domain before pagination; the adapter repeats the case-insensitive substring check on returned names instead of assuming the upstream operator preserved its exact semantics. `source_total` therefore counts the filtered server domain. Filtered results do not claim that opening the unfiltered collection route reproduces the result. Pagination is bounded and rejects malformed or repeated record identities; matching a count establishes coverage of that scan, not database snapshot consistency under concurrent updates.

## Provisioning the fixture

One command builds the whole thing from nothing — the Odoo, its database, the addon that
serves PageController, the credentials and the snapshot the gate restores to:

```bash
python -m web_operation_agent_odoo.deploy provision
```

It needs Docker and a checkout; it prints the configuration lines to append to `.env` and
takes a few minutes, most of it Odoo building a demo database. Seven steps, each runnable on
its own when one of them is what you are debugging:

| Step | What it does |
|---|---|
| `assets` | Downloads the pinned PageController tarball, checks its npm integrity, extracts the three files a deployment serves and checks each one's sha256 |
| `up` | Starts Compose and waits until Odoo answers, not until Compose returns |
| `database` | Creates `woa_reference` from Odoo's own demo data, installs `sale_management`, `account` and the Runtime addon, then asks the running server for the asset and hashes what it actually served |
| `source` | Copies the compiler's reading material out of the image — 570 files, the ten addons whose descriptors the packaged Skills cite |
| `credentials` | Mints an API key through the container's own Odoo shell and captures a browser session |
| `baseline` | Records the snapshot the gate restores to |
| `env` | Prints the configuration |

The order matters in exactly one place, and it is the reason `provision` exists rather than
a list of instructions: **the snapshot has to be taken after the API key is minted.** A
restore puts the whole database back, so a baseline recorded first would delete the
credential on the gate's first run and the fixture would work exactly once.

Everything about it is loopback-only and refuses to be otherwise. It writes a master
password in the clear and types Odoo's own well-known demo password, so every step that
does either checks the host first. It is a disposable application to point the agent at.

`down` stops it; `down --destroy` discards the volumes as well.

### What is not committed

`page-controller.js`, its mask chunk and its MIT `LICENSE` are fetched from the pinned npm
release and checked against
[`policies/page_controller.json`](../../src/web_operation_agent/policies/page_controller.json),
never tracked here. This repository serves that asset; it does not redistribute it, and a
committed copy would drift from the pin without anyone noticing. The extracted Odoo source
is not committed either — it is 570 files reproducible from the pinned image, and the
release string the compiler binds to is derived from that image's digest, so a fixture
rebuilt on a newer `odoo:18` gets a different release and the citation gate notices.

`stock` is deliberately not installed. With it, 22 of the 36 saleable demo products invoice
on delivery and nothing has been delivered, so no order is left invoiceable and the gate's
invoicing case has no target. It is still compiled — citations reach it — just not
installed, and without it the fixture lands exactly where the qualified baseline was:
S00001/S00002/S00003/S00005 draft, S00007 the one order with something to invoice, Acme as
partner 10, four customer invoices.

## Driving it by hand

After `provision`, this opens a real Chromium on the Odoo web client with the assistant
panel mounted, and waits for you:

```bash
python -m web_operation_agent_odoo.demo --allow-mutations
```

No flags to set: every path comes from the `.env` that `provision` printed, and the provider
credentials come from the same file.

The panel opens against the right edge, a little below the middle of the page, already
expanded, showing three suggested goals and a composer. Type a goal; close the window to end the
session. Each turn prints one JSON line — the answer, the approval counts, the page actions
— and the run directory keeps the audit ledger.

(In a real deployment the panel remembers whether you left it open and otherwise sits as a
small launcher in the corner. A demo runs in a throwaway browser profile with nothing to
remember, so it seeds that state open: a demo you have to hunt for is one that looks
broken.)

Things worth trying, in the order that shows the most:

| Type this | What to watch for |
|---|---|
| `哪些报价单还没确认？` | The answer is the typed projection, not the model's prose: a table of the records with the source, scope and observation time under it. `source_complete` in the audit line says the scan saw the whole collection |
| `打开 Acme Corporation 的客户记录` | It queries first and navigates to the exact route the tool returned. It cannot invent a URL |
| `填写客户名称 NovaForge Labs 和邮箱 ops@novaforge.example，但不要提交` | The server opens the new-customer form, fills only those two application-issued bindings, then stops with Save still under your control |
| `把报价单 S00002 确认掉` | **The approval card appears and waits for you.** Read the effect and the target, then click it — or dismiss it and watch the write not happen |
| `删除客户 Acme Corporation` | Refused — there is no delete tool on the surface, so the refusal is the product's rather than the model's good manners — but it opens that customer's record first and says the deletion has to be made by hand there |
| `把报价单 S00001 也确认掉`, then ask again | The second identical request meets the journal, not the application |

`--allow-mutations` offers the three typed write tools. It does not make writes automatic:
each one still needs a fresh approval clicked in the page, and composition refuses to enable
them at all without a durable journal.

Without `--allow-mutations` persistent business tools remain read-only. The transient customer
form handoff above is still available because it leaves the record unpersisted.

## Running it non-interactively

Against the provisioned fixture, or against your own Odoo with the pinned page controller
served from its origin — any module directory on the addons path works, for example
`<addons>/woa_runtime/static/src/page-controller.js`:

```bash
python -m web_operation_agent_odoo.demo --scripted --headless \
  --api-key .private/odoo-api-key.json \
  --storage-state .private/odoo-storage-state.json
```

`--scripted` uses a fixed walkthrough with no provider and no spend: it exercises real Odoo,
real Chromium and the real product turn, and asserts nothing about a model. Drop `--scripted`
and set `AGENT_OPENAI_BASE_URL`, `AGENT_OPENAI_API_KEY` and `AGENT_OPENAI_MODEL` to run
with one.

The gate version writes a schema-fixed artifact of counts, digests and booleans, and checks
that the fixture is byte-identical before and after:

```bash
python -m web_operation_agent_odoo.demo.qualify \
  --api-key .private/odoo-api-key.json \
  --storage-state .private/odoo-storage-state.json \
  --output .artifacts/odoo/live-qualification.json
```

`scripts/qualify.sh` runs it whenever `WOA_ODOO_BASE_URL`, `WOA_ODOO_API_KEY_FILE`,
`WOA_ODOO_STORAGE_STATE` and `WOA_ODOO_PAGE_CONTROLLER_MODULE` are set; without them the gate
reports BLOCKED, which is not a pass.

## Session parameters

The adapter declares `session_id`, `access_token` and `token` through
`ApplicationRoutePort.session_handoff_query_keys`. Odoo 18 does not put its session in a URL,
so no concrete leak exists on this release — but older Odoo did, portal links still carry
`access_token`, and the core's static redaction baseline has never heard of any of them. A
composition must therefore build its provider egress policy from the adapter:

```python
ProviderEgressPolicy(session_handoff_keys=adapter.ports.routes.session_handoff_query_keys)
```

`AgentTurnExecutor` refuses to compose without it.

## Tests

```bash
PYTHONPATH=src:examples/odoo/src python -m unittest discover \
  -s examples/odoo/src/web_operation_agent_odoo/tests -t examples/odoo/src
```

Every case runs against a fake JSON-RPC transport or an invented two-module source tree. No
test in this package requires a reachable Odoo; the live checks are the opt-in gate above.
