# web-operation-agent-odoo

Odoo 18 support for [`web-operation-agent`](https://github.com/): the `/odoo/<path>` route
grammar, a source compiler that reads Odoo's own module XML and model Python into a cited
`SourceMap`, a generic allowlisted code-block index, typed business tools over the delegated
JSON-RPC API, and the deployment
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
3. `server/reads.py`, `server/generic_reads.py`, and `server/mutations.py` implement bounded
   domain reads, catalog-bound generic reads, guarded writes, and application-owned
   verification.
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

`OdooCompiler` reads menus, actions, views, model fields, method documentation, and
user-visible refusal conditions into `SourceRelation` triples, each with a citation that
`SourceTree.verify` can re-check against the exact bytes. What that buys is the class of fact
a page cannot show. On a confirmed sales order, two buttons are both labelled "Create
Invoice":

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
capability = adapter.source_capability(
    source_root, release="18.0-20260908+…", deployed_commit="<full commit id>")
config = AgentTurnConfig(..., source=capability)
```

`deployed_commit` is optional. It enables local `CODEOWNERS`, blame, file-commit and
deployment-to-target diff metadata only when `source_root` is a Git worktree whose
allowlisted bytes exactly match that commit. An image extraction has no `.git`; source
answers still work and history lookup explicitly reports unavailable.

The add-on supplies the running deployment's installed-module manifest before compilation.
The map and raw-code index exclude modules that merely exist in the image, and their release
combines the image release with the installed-module digest. The capability goes into the
turn as one value; `lookup_source` combines compiled rule relations with deterministic local
BM25 over allowlisted Python, JavaScript, XML and access declarations. It mints turn-owned
evidence. Source-backed answers show a concise conclusion rather than cited bytes, paths,
or hashes; the source reference and an exact checked supporting span remain in the
server-side answer record and audit. The span check verifies provenance, not the semantic
correctness of the model's paraphrase.
Raw blocks are labelled implementation, not current state. Static permission declarations
still require an actor-scoped runtime check, and source cannot prove the cause of one actual
record event.

The same add-on exposes a bounded actor-scoped model/action catalog. It includes window
actions without menu entries when the current user's groups and target-model read access
permit them; menu entries only supply breadcrumbs. Catalog generation itself reads no
business records and never grants mutation authority. The Odoo adapter maps
that framework-specific catalog into the core entity-read contract, whose same three schemas
also run over the synthetic Contacts REST adapter:

1. `discover_entities` searches readable, action-backed, non-transient business entities and
   returns catalog-bound opaque entity IDs, not technical model names;
2. `describe_entity` resolves one current entity; the Odoo adapter calls `fields_get` under
   the same delegated actor, removes sensitive names and unbounded relation/binary/HTML fields,
   then returns the core's opaque field IDs;
3. `query_entity_records` accepts only those current IDs, at most eight AND conditions and
   eight projected fields, a 20-row page and a bounded offset. The core fixes the limits; the
   Odoo adapter translates generic operators, fixes ordering to `id asc`, runs exact matching
   and actor-visible total counts, and returns query plus field-level record evidence.
4. `resolve_record_reference` takes an entity description and a name or document number,
   then uses the delegated actor's native name search across at most three catalog matches.
   It returns at most five record candidates and does not apply the narrower customer-rank
   filter. Ambiguity is retained; no empty lookup proves global nonexistence.
5. `find_page_entries` ranks the actor's window actions, including entries without a menu.
   Only a reviewed destination independent of `active_id`/`active_ids`/`active_model` gets
   a route; all other candidates remain informational. A discovered action does not grant
   navigation or bypass the normal page policy. With `detail_query`, it also ranks form
   fields and notebook tabs from the delegated actor's default form view and `fields_get`.
   These are location hints only; an action-specific view and live visibility can differ.

The action is a discovery hint, not a query scope: its dynamic domain/context is never
evaluated by this path. A generic query therefore covers the actor-visible model rows named in
the result, not necessarily the rows shown by one menu action. Catalog changes make old
entity IDs stale; `fields_get` changes make old field IDs fail. Raw model names, method
names, arbitrary domains, OR/NOT expressions, caller-controlled ordering, grouping, writes,
transient models, settings/users/messages/attachments, and unbounded fields are not accepted.
Domain-specific typed tools remain preferable when their narrower business semantics apply.

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

### Which Skills stay, and why (2026-09-23)

Phase 8 asked whether deployed source removes the need for hand-written Skills. It does not remove these, and the verdict is per Skill rather than for the catalog as a whole. Tools are registered by the adapter independently of Skills; an eligible tool does not require a Skill to be loaded. Skills supply optional business guidance, never authority.

| Skill | Verdict | Why |
|---|---|---|
| `customers-query`, `products-query`, `quotations-query`, `sale-orders-query`, `customer-invoices-query` | Keep | These explain the typed reads' business scope. The quotations one also carries organizational semantics no source states: this deployment counts only draft orders as quotations, and that is the one task the four-arm run's Skill arm won outright. |
| `runtime-records-query` | Keep | The generic entity read for everything without a typed collection. Three tool schemas serve every entity, so this is what keeps the surface from growing with the application. |
| `customers-draft`, `customers-edit` | Keep | Form preparation, read-only. They hand the person a filled form to save themselves. |
| `customers-create`, `customers-archive`, `sale-orders-confirm`, `sale-orders-invoice` | Keep | These describe business procedures around registered writes. The tool contract, actor scope, precise proposal, fresh approval and readback authorize and verify each operation independently of Skill loading. Source can explain the operation but cannot authorize it. |

Nothing here is removed on the strength of "source could derive it". A Skill can still provide organizational meaning, such as which records count and how a procedure should be explained. That knowledge is separate from the registered tool and its permissions.

## Tools

| Tool | Model | Scope | Route |
|---|---|---|---|
| `query_sale_orders` | `sale.order` | confirmed orders (the Orders page's own domain) | `/odoo/orders` |
| `query_customers` | `res.partner` | customer rank above zero | `/odoo/customers` |
| `query_products` | `product.template` | saleable products | `/odoo/products` |
| `query_quotations` | `sale.order` | draft and sent quotations | `/odoo/sales` |
| `query_customer_invoices` | `account.move` | customer invoices, excluding credit notes | `/odoo/customer-invoices` |
| `discover_entities` | opaque catalog entity | current actor's readable, action-backed non-sensitive entities | — |
| `describe_entity` | opaque catalog entity | safe `fields_get` translation under the current actor | — |
| `query_entity_records` | opaque catalog entity | bounded actor-visible records; exact counts; fixed order | — |
| `resolve_record_reference` | opaque catalog entity | bounded actor-visible name/number candidates | eligible record route, if available |
| `find_page_entries` | actor-visible window action | bounded page candidates, including no-menu actions | reviewed standalone route only |
| `prepare_customer_form` | `res.partner` | exact customer form draft; transient until user submission | `/odoo/customers/new` |
| `prepare_customer_edit_form` | `res.partner` | exact existing-customer fields; confirmed before Odoo can autosave | `/odoo/customers/<id>` |
| `create_customer` | `res.partner` | one customer, opt-in | `/odoo/customers/<id>` |
| `archive_customer` | `res.partner` | one active customer, opt-in | `/odoo/customers` |
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
| `source` | Copies allowlisted structured and raw-code reading material out of the image for the ten source-qualified addons; keeps each addon's `i18n/zh_CN.po` for label names and excludes other translations, tests, demo/seed data, secrets and vendored frontend libraries |
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

An existing fixture keeps working after the addon changes: Odoo loads the addon's code from disk, and `woa_runtime` 1.5's page write guard creates its one table the first time the Runtime opens a window. One web-client write the guard refuses quietly: opening a record that has an attachment makes the client mark it as the record's main attachment (`ir.attachment.register_as_main_attachment`); nothing is saved, and the client gets an empty result instead of the refusal dialog, which would otherwise cover the page and its controls. Restart Odoo to load new backend asset declarations, including the search-model facet bridge; the local pilot loaded it on restart without a database module upgrade. `-u woa_runtime` records a new module version when needed; it changes `ir_module_module`, which the P8.7 fingerprint includes.

The guard uses per-browser-session transaction advisory locks: normal requests share the
lock through commit; `/begin` takes it exclusively before acknowledging the read-only
window. This fences requests already in flight. It does not stop effects outside the
database or a client retry made after `/end`.

### What is not committed

`page-controller.js`, its mask chunk and its MIT `LICENSE` are fetched from the pinned npm
release and checked against
[`policies/page_controller.json`](../../src/web_operation_agent/policies/page_controller.json),
never tracked here. This repository serves that asset; it does not redistribute it, and a
committed copy would drift from the pin without anyone noticing. The extracted Odoo source
is not committed either — it is reproducible from the pinned image, and the
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
| `归档客户 Acme Corporation` | It queries the exact active customer first, then shows an approval card; after approval it verifies the record is inactive and does not try to open the archived record |
| `删除客户 Acme Corporation` | Refused — archive is reversible and explicitly supported, while permanent deletion is not on the tool surface |
| `把报价单 S00001 也确认掉`, then ask again | The second identical request meets the journal, not the application |

`--allow-mutations` offers the four typed write tools. It does not make writes automatic:
each one still needs a fresh approval clicked in the page, and composition refuses to enable
them at all without a durable journal.

Without `--allow-mutations` persistent business tools remain read-only. The transient customer
form handoff above is still available because it leaves the record unpersisted.

## Running it non-interactively

Against the provisioned fixture, or against your own Odoo with the pinned page controller
served from its origin — any module directory on the addons path works. The Odoo
distribution loads `<addons>/woa_runtime/static/src/page-controller-with-state.js`,
which wraps the pinned `page-controller.js`, reports active search facets, and exposes
bounded filter/group choices from the current search model. It also emits Odoo's required
`change` event after a successful existing-customer edit fill: without it the form can show
the new value in the DOM yet save the old one on leaving. Demo and qualification defaults
use this wrapper; set `WOA_ODOO_PAGE_CONTROLLER_MODULE` explicitly only when the
deployment serves it at another path:

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
that the fixture is byte-identical before and after. It also runs a zero-model generic-read
probe against Journal Items: browser/API identities are bound, only `generic_records:read` is
granted, and the artifact retains counts, evidence cardinality and latency rather than record
values:

```bash
python -m web_operation_agent_odoo.demo.qualify \
  --api-key .private/odoo-api-key.json \
  --storage-state .private/odoo-storage-state.json \
  --output .artifacts/odoo/live-qualification.json
```

The experimental no-Skill form surface has a separate no-provider gate. It visits the real
customer, product and quotation create pages, asks the actor-scoped metadata/page
intersection for opaque bindings, prefills six scalar fields, and checks the live DOM plus
before/after record counts without clicking Save:

```bash
python -m web_operation_agent_odoo.demo.form_qualify \
  --api-key .private/odoo-api-key.json \
  --storage-state .private/odoo-storage-state.json \
  --output .artifacts/odoo/form-qualification.json
```

For an HTTPS loopback gateway whose private CA is not installed in Chromium, add
`--ignore-browser-https-errors`; the command rejects that option for non-loopback hosts.
The form port has no adapter-wide enable switch, but only the exact fields in the three
browser-qualified standard-module cases are eligible by default. An unknown create page
or field returns `form_available=false`; `fields_get` alone is not a safety certificate.
This gate checks the pinned cases and no-submit boundary, not arbitrary installed modules
or custom `onchange` side effects. Additional fields need application-owned qualification
before they can become prefill drafts.

`scripts/qualify.sh` runs both live gates whenever `WOA_ODOO_BASE_URL`, `WOA_ODOO_API_KEY_FILE`,
`WOA_ODOO_STORAGE_STATE` and `WOA_ODOO_PAGE_CONTROLLER_MODULE` are set; without them the gate
reports BLOCKED, which is not a pass.

## Goal turns

`OdooGoalPort` (`web_operation_agent_odoo.goal`) implements the core's `GoalPort` for the
goal-directed turn. Its index is compiled once per adapter from the runtime descriptor
catalog, `get_views` (with each action's own search view) and `fields_get`, all under the
delegated user: readable, non-transient window actions become pages, minus the framework's
own modules and actions whose domain or context needs a current record; the models of
menu-reachable pages become detailed entities with their list and form fields, form tabs
and header buttons, other models appear by label only. Chinese names for entities, pages,
fields and choices come from Odoo's own `i18n/zh_CN.po` catalogues under the source root,
read through a translation-only allowlist separate from source retrieval; without them the
index is English only. The reference deployment's conventions, what its organisation means
by 订单, 销售订单, 报价单, 发票 and 退款, are in `conventions/reference.json` (a draft awaiting
the owner's review); a deployment passes its own file, and the demo takes
`conventions_path=None` for none.

Resolution is read-only JSON-RPC: `name_search` for a reference (an exact display-name match
wins, a company beats its contacts, several remaining candidates are returned as ambiguous),
`read` for facts, `search_count`, `read_group` and `search_read` for counts, sums, groups and
lists over the page action's literal domain plus the declared conditions (a page's
`search_default_*` filters apply unless they name the current user), and record routes from
the first form-capable action whose literal domain admits the record, else `/odoo/<model>/<id>`.
A record's tab and a handoff control are named for the core, which selects or highlights them
on the live page. The demo enables this path with `goal_turns=True` (the default) for
read-only provider sessions.

Lines: for each menu-reachable model, the one2many fields its form embeds with a nested list
(`order_line`, `invoice_line_ids`) become line entities, with the parent link first and the
nested list's fields after it, so "the lines of S00004" is a list or count over
`sale.order.line` with `Order Reference = S00004`. Dates: a datetime condition is bounded at
the user's local midnight converted to UTC (Odoo stores UTC); a date field compares as a
calendar day; a search-view filter whose domain calls `context_today()` or `time.strftime()`
is evaluated with the turn's date. A `search_default_*` whose filter the served search view
does not carry falls back to the selection key of the same name (`in_invoice` on Bills). An
aggregate declared without a page counts every record of the entity the user may read; a
declared page adds its action domain and its non-personal default filters.

A record given as conditions instead of a name (`record` empty, `metric.filters` on
customer, amount, status or date, `page.key` for the scope) is located by `search_read`
within the page's domain and non-personal defaults, then beyond the defaults and beyond the
page's domain when nothing is inside; exactly one row is the record, none or several is a
problem with the candidates, and `sort` picks the first of an ordered match ("the higher
one"). On a line entity, a condition on the link to the owner whose value the link cannot
find (an invoice number on order lines, "Move" for the journal entry, an amount or a date
that describes the order) is rewritten onto the owner found by that value, on that owner's
own line entity. A bare number as search words is the record's total, or the first money field the pages show that some record carries. A problem the declarer could fix carries a `repair` hint on the resolved deliverable (the entity's field labels, the page's filter labels, the records a description matched); the core shows it to the model for its one second declaration. A handoff's `value` becomes the plan's `fill` when the field can take it with certainty: a date at day precision (ISO), a selection by its key with the label as fallback, a many2one by the exact name of one existing record (typed, then chosen among the field's suggestions), a number, or text; a checkbox or an unknown record name yields no fill and, for a record, a repair hint.

The zero-model matrix (`python -m evals.odoo.goals.resolver_matrix --credentials
.private/odoo-reader`) resolves 184 hand-written declarations against the reference fixture
and checks values, counts, sums, groups, routes, tabs, controls and honest problems. Run it
before any model-backed evaluation; its relative-period cases assume the fixture's
September 2026 data and clock.

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
