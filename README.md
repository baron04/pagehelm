# Web Operation Agent

[简体中文](README.zh-CN.md)

Web Operation Agent is an alpha framework for embedding an AI assistant in a first-party
web application. Planning, model access, credentials, policy, approvals, and audit state stay
on the server. A small browser Runtime observes the current page, executes fixed generic
actions, displays progress and confirmation UI, and returns receipts.

The repository has one public reference implementation: [Odoo 18](examples/odoo/README.md).
It is not a hosted service, a general browser extension, or a production-ready autonomous
agent.

## Current capabilities

- A goal-directed turn asks the model once: given the application's index (entities,
  fields, pages, views, filters, groups, sections and controls, compiled by the adapter
  under the acting user), declare what the person wants. The server then resolves the
  declaration without further model calls: records by the application's own name search,
  field values by typed reads, counts and sums by scoped queries, pages by reviewed action
  routes, tabs by their visible name, filters and groups by the page's own facet state, and
  a requested change by highlighting its control for the person. Every page switch is still
  confirmed, every page command runs under the application's page write control, and no
  control that could write is pressed. The reply is rendered from resolved values, never
  from model prose, so a fact the turn reports is one it read. The Odoo demo and the goal
  evaluation runner take this path for read-only sessions; mutation sessions and the
  scripted apparatus keep the step-by-step loop below. The declaration is one model output
  and can name the wrong entity, field, scope or search words; the resolver reports those
  as not done rather than guessing, apart from three deterministic fallbacks (a reference
  found on an entity that links to the named one, a label containing a link field's label,
  the page's own "My …" facet when the person asks for everyone's records), and a zero
  count inside a page's scope is reported together with the entity-wide count. Conditions
  take comparison operators on numbers, dates and text, including a prefix ("starts with"); a date condition accepts a day,
  month, year, range or a named period (today, this month, last quarter, last 30 days,
  overdue), and a page filter that names "today" is evaluated for the turn in the user's
  time zone. The lines a record's form embeds (order lines, invoice lines) are entities of
  their own, so they can be listed, counted and summed by their link to the record, and
  the owner may be given as a condition the link itself cannot resolve (an invoice number
  on order lines, an amount or a date that describes the order). A record the person
  describes rather than names ("the quotation sent to Joel Willis", "last month's
  customer invoice", "the 31,750 credit note") is picked out by its conditions within the
  declared page's scope, or beyond the page's defaults when they hide it; only exactly one
  match is acted on, and none or several is said so, with the candidates. A named record
  that does not exist is never turned into a creation; a creation opens the page declared
  for the entity, else its widest page. A list that finds exactly one record binds a
  later deliverable of the same turn that names none. The declaration is made knowing the
  application's current date, so a month or day said without a year lands in the right
  year. When the resolver finds something a different declaration would fix (a field the
  entity does not have, a description that matched several records, a change asked for
  with nothing to point at), it says so in the index's terms and the model declares once
  more; the second declaration is taken only when fewer deliverables stay unresolved, and
  there is no third. A handoff that carries the value the person gave for a field enters
  it first (typed text, a date at day precision, a choice the field offers, or a linked
  record typed by name and chosen among the field's own suggestions) and checks what the
  field then shows; saving stays with the person, the page write guard holds throughout,
  and the reply says to discard the change if it is not wanted, since the form saves
  itself on leaving. A value that cannot be entered with certainty is left to the person
  and said so. Field labels match by whole words
  or by the field's own name, never by substring. A view the page lacks is stated in the
  reply while the page opens in its own view. A condition on an unstored computed field
  is refused rather than silently ignored, unless its value names one of the page's own
  filters. The resolver's reach is measured by a zero-model matrix over the reference
  fixture (`evals/odoo/goals/resolver_matrix.py`: 184 declarations across entities,
  deliverable kinds and condition types, all read-only RPC). Odoo implements the full
  reference index and resolver; the structurally different Contacts REST test application
  implements the same core port for actor-scoped facts, counts, lists and basic pages, and
  explicitly refuses unsupported page controls and handoffs. The port contract has two
  levels: the required index, records, facts, counts, lists and page addresses, and
  optional capabilities a port declares by name (page filters and groups, tabs, embedded
  lines, controls to point at, value entry, page search, source rules); a deliverable
  that needs an undeclared one is answered with what the application does not offer,
  without asking the port. The Contacts application declares tabs and handoffs and takes
  goal turns in its demo. A turn is marked complete only when every declared deliverable
  settles. A staged declaration path exists behind a switch (`staged=True` on the executor,
  `--staged` on the goal runner): the request's hard references (document numbers, codes,
  Latin-script names) are cut out by shape and located by the adapter, the model first
  picks entities and deliverable kinds from one ranked line per entity, and then declares
  the details against those entities' neighbourhood of the index (their links, lines and
  owners, a few KB rather than the whole index). Words the request shares with an entity's
  label only order that list; nothing is dropped. The path is measured against the single
  declaration before it becomes the default; a zero-model replay of recorded runs
  (`evals/odoo/goals/reference_recall.py`) reports how many declared records the reference
  cut recovers.
  If the model declares a write intent but only resolves read-only pages or facts, opening
  those pages is reported as partial guidance, never as the requested change having occurred.
  For a new record, an adapter may expose an actor-scoped `create` operation on a collection
  page; the goal turn can open that page and highlight the live entry control, without
  searching for the proposed name as an existing record or pressing the control. A completed
  handoff is never recorded as a claim that the requested write happened.
- A server-side ReAct loop receives every tool result and a fresh page observation before
  its next decision, with step, time, token, and provider-spend limits.
- Application-owned Skills and typed tools provide current business facts. When explicitly
  enabled, deployment-bound structured facts plus a language-neutral local code-block index
  can answer source questions while retaining exact file, line, release, and byte-digest
  provenance on the server rather than showing source excerpts in the user answer. For a
  source paraphrase, the server also checks that a short supporting span occurs in the cited
  evidence; that check does not prove the model's interpretation of the span.
  An optional local Git provider separately cites ownership, blame, commit, and version-diff
  metadata bound to the deployed commit. Source retrieval remains optional and is never a
  prerequisite for ordinary browser or API operation. A lookup that informs an operation
  does not itself turn the operation into a source answer: the action report comes from
  an observed runtime receipt. Source answers still require checked supporting evidence.
- An application-neutral entity-read contract can discover a current actor's readable
  business entities and perform bounded reads without adding one Skill per entity. The Agent
  receives only catalog-bound opaque entity and field IDs; an adapter translates its ORM,
  REST, OpenAPI, or other native metadata behind the contract. The core validates an
  AND-only filter AST and pagination bounds; the contract requires stable adapter-owned
  ordering and exact counts. Odoo and the structurally different synthetic Contacts REST
  adapter run the same contract tests. When one answer combines different read scopes,
  numeric headlines identify their own scope; this does not prove that the Agent selected
  the scope the user intended. The Odoo reference also opts into a narrow count/sum
  answer check: before its first business read the Agent declares the requested read
  collections and measures; the server binds computed numbers to the executed query and
  refuses a complete answer that substitutes another collection. This does not yet verify
  that the declared collection, subject, or filters match the user's intent.
  A separate `resolve_record_reference` read can locate bounded name/number candidates in
  actor-readable entities without first calling discovery, field description, and a record
  query. It returns ambiguous matches rather than selecting a record, and an empty result
  is not a global nonexistence claim. Odoo uses the actor's native name search without the
  narrower `query_customers` collection filter; Contacts implements the same contract over
  its REST data. This is a record-location primitive, not a completed-answer or page-action
  guarantee.
- A separate `find_page_entries` read ranks current-actor page/action metadata, including
  entries with no menu breadcrumb. It returns a route only for an adapter-reviewed,
  standalone destination; an unreviewed or current-record-dependent action is information
  only. Ordinary page policy and fresh page-state verification still govern navigation.
  Odoo translates its window-action catalog and Contacts its REST application's page map
  through the same core contract. An optional detail query also ranks form-field and tab
  hints for at most the first two openable candidates from actor-visible adapter metadata,
  without adding another Agent tool. Odoo uses the actor's default form view and field labels;
  Contacts uses its page map. These are
  not selectors or evidence that the requested part is present on the live, possibly
  action-specific view. This does not make every application page operable.
- A read-only `resolve_goal_candidates` call can combine up to three answer, page or
  handoff candidate lookups using those existing actor-scoped entity, record, field and
  page catalogs. It neither chooses a candidate nor freezes a complete user goal; the
  Agent must still bind the intended scope, read or observe the result, and verify the
  final state. This composition has passed zero-model Odoo and Contacts checks, but has
  not been shown to improve model accuracy or end-to-end latency.
- An optional application-neutral current-form contract supports a no-Skill fallback for
  safe scalar form filling. `describe_form` intersects the latest viewport controls with
  actor-scoped adapter metadata and returns only short-lived opaque IDs; `prepare_form`
  rechecks both sides and retains the exact native bindings on the server; `prefill_form`
  types that draft, checks the live field values and, where an application declares a
  read-only window, requires its acknowledgement of release before ending the turn.
  It never clicks or submits the form.
  A failed handoff does not claim the form is ready; some fields may already contain the
  draft. Odoo and Contacts translate different metadata sources to the same contract.
  An optional source lookup can inform a value for an existing field; a source-derived
  value carries a turn-local reference
  and exact supporting span for internal audit. This verifies provenance, not the model's
  interpretation. Source cannot create a field, selector, choice, permission, or write
  authority.
  The Odoo reference translation is available by default only for the exact scalar fields
  covered by its three standard-module browser cases (customer name/email, product
  name/price/invoicing policy, quotation expiration). Other create pages or fields are not
  promoted to safe drafts from `fields_get` metadata alone: arbitrary deployment-specific
  `onchange` code may have external effects that a read-only database transaction cannot stop.
  The separate existing-customer edit path requires approval before filling. Its Odoo
  controller adapter delivers the form's `change` event so a value visible in the DOM also
  enters Odoo's pending record; the reference pilot verified an email remained unchanged
  until the person left the page, then saved. This does not qualify arbitrary edit forms,
  in-flight requests started before the guard, or external effects.
- Turn context is assembled by source role: the current page is the live grounding baseline;
  bounded conversation history is included for referential or clarification follow-ups;
  Skills are selected from the current task; and deployment source remains a lazy cited
  lookup instead of being injected wholesale.
- Browser actions are checked against the current observation and application route policy.
  Page observations are viewport-bound and expose at most 200 live controls; a bounded
  read-only scroll obtains the next fresh observation instead of scanning an unbounded DOM.
  Page switches require a visible confirmation.
  The answer is spent only by the switch it was asked for, so when answering it closes the menu the switch was in, reopening the menu and picking the same item does not ask again.
  A successful same-page click can be
  reported as an interaction only when a fresh observation shows a changed page; this
  receipt is not proof that a business goal was completed. After an observed action,
  answer-format repair cannot dispatch another page action. Source-assisted page loops
  that revisit the same facts and states stop without claiming completion.
- For a page operation the assistant cannot perform, `highlight` can mark one named,
  currently visible control and end the turn without clicking or submitting. The server
  checks the latest ready observation, any declared page target, and the fresh action
  receipt before saying where to continue. A matched typed mutation remains the path for
  an actual write; a marker cannot substitute for it. If the application declares a
  page write guard, the Runtime requires its explicit release acknowledgement before
  reporting the handoff ready. A real page gesture or closing the panel removes the marker;
  a gesture during an active turn fences later Agent actions and requests cancellation.
  This is a guided handoff, not a claim that the business change happened.
- For a request that names a page state -- open a screen, switch a tab, apply a filter
  or grouping, arrive at a record --
  the Agent declares that state up front with `set_target`: an exact same-application path
  and/or the accessible names of controls that must show as selected, or application-reported
  active filters and groups, or a filter previously observed active on that page that must
  become absent. Explicit requests to filter, group, or remove a filter cannot be settled
  with a path-only target. After every successful
  write-free page action the server compares the fresh observation with the declaration. If
  a single-page application has changed its URL but still exposes the prior page projection,
  the embedded Runtime marks rendering pending and the server does not report arrival; a
  later observation must show the replacement view. An application adapter may also report
  a blocking page error; that state cannot settle a page target. When the target matches a
  ready observation, the server reports the reached state. An explicit request for a conventional
  List, Kanban, Calendar, Pivot, or Graph view also requires that view to be observed,
  even if the Agent declared only a path. An application may report its actually rendered
  view without a visible view-switch control; a URL or candidate alone is not evidence.
  A fresh observation may be needed after the switch.
  For a pure navigation request it accepts
  only `finish` and `set_target` (other calls are refused without dispatch; the tool list stays
  the same for prefix caching). A request that also needs a fact may continue reading or
  observing. The sentence the user reads is composed from the observed
  page, never from model prose. A declared target grants no permission: clicks and navigation
  are routed, confirmed, and settled exactly as before, and a write in the same window is
  settled by the write path instead. The server never ends the turn on a target by itself,
  because it can verify a page state but not that the state is the whole request. A question
  about where something is kept -- which screen, list or setting -- is answered by opening it,
  with where it sits in the menus when a cited source or Skill says so.
- Generic click can stay disabled as an action while an application re-allows part of it: an optional click policy sees the current observation and the element named and either allows that click or refuses it before dispatch. The Odoo reference integration allows tabs, the Home button, same-application links to reviewed pages and records, and named view-switch, pager, search and menu controls on those pages; reviewed lists can also open their rows or cards. On a record it normally allows only the pager, top menus, and a link to a related record. One reviewed exception on customer invoices opens the Pay dialog: the record route, visible button name, and live button method must all match the adapter review. It does not click Create Payment or grant write approval. Other header workflow buttons, New, Actions, row selection, inline editing, module activation, unnamed buttons, and unreviewed pages remain refused. The review lives in the adapter and names actions by XML ID, resolved (with their URL paths) against the deployed database. A policy may also name the pages it reviewed as destinations, which the assistant may then open by address rather than by finding the menu; Odoo allows a reviewed page or a record opened from one, with at most a `view_type` query. A tab that marks itself current only by class, as Bootstrap's `.active` does in Odoo 18 notebooks, is reported by the Runtime as `selected=true`, so a declared tab can be verified.
- An integration with a page write guard may also expose up to 32 application-owned filter and group choices in the current observation. The optional `apply_facet` tool accepts only a current choice ref and a changed active state; the server checks the page policy, and the Runtime checks that the choice still has the observed meaning before dispatch. Odoo obtains choices from its current search model on reviewed collection pages; an unreviewed page exposes no usable facet action. A fresh observation of applied state, not the candidate list or the action receipt, settles the target. This does not install business tools or require a Skill. Restart Odoo after deploying the new backend asset; the reference pilot loaded it on restart without a database module upgrade.
- An application can also make the signed-in session read-only while the Agent acts on the page. It declares a `page_write_guard` path on its descriptor; the Runtime posts to `<path>/begin` before every page action and refuses the action unless the application confirms the window, and posts to `<path>/end` when the turn ends. Odoo's `woa_runtime` 1.5 implements it: while the window is open every request of that session runs in a PostgreSQL read-only transaction and a write is refused with nothing saved, so a click the review missed -- or an autosave the web client makes on its own -- cannot change data. A page action that loses its result while the window holds, such as a menu button the model took for a link, is therefore settled as no write rather than as an uncertain one, and the turn continues; the receipt says the window held, and the server honours that only for an application that declares a guard. Deploy the 1.5 addon code before this adapter: against older addon code the Runtime cannot open the window, so every page action is refused.
- The Runtime also reports the sole active button in a visible multi-button navigation group as selected, independently of the bounded action-ref list; it does not treat a standalone active action button as a selected view. These bounded names are observation facts, not clickable refs or permissions.
- When no Skill trigger matches a request's wording, the catalog's read-only fallback stays
  loadable but is presented as an unmatched index rather than as task candidates. A typed read
  whose result repeats one already returned in the same turn is answered with a short notice
  instead of the full result, and a third such repeat ends the turn as `no_progress` without
  claiming completion; a completed write resets this.
- Registered typed tools do not require a matching or loaded Skill; Skills provide optional
  business guidance, not tool installation or authorization. Tool schemas are filtered by
  deployment write enablement and current actor scope, then checked again at dispatch.
- Writes are disabled by default. Core turn configuration initially disables every browser
  action that can initiate an application write, including navigation; an adapter must
  explicitly opt into its reviewed subset. An enabled write binds its exact effect and
  targets to a fresh one-use approval, dispatches at most once, and is verified through the application.
  A timed-out or otherwise uncertain write is recorded as `indeterminate` and never retried
  automatically.
- A short-lived actor/tenant/workspace principal can be bound to the browser capability,
  typed-tool scopes, approvals, mutations, audit records, and revocation state.
- The React assistant panel is a view over server state. Its copy, bounds, state names, and
  palette come from server-owned contracts.
- Optional SQLite stores provide session ownership, progress outbox, bounded conversation
  context, mutation journals, backups, and explicit operator settlement.

These are implemented-path claims, not a general success-rate claim. The repository has no
public benchmark large enough to establish superiority over another browser agent.

## Architecture

```text
First-party page
  assistant.js + panel + fixed browser tools
                    │ authenticated observations/actions/decisions
                    ▼
Server composition
  session policy · planner/model · approval · audit · durable state
                    │ typed application ports
                    ▼
Application adapter
  identity · routes · reads · writes · verification · optional source map
```

The core package is application-neutral. Application behavior enters only through adapters,
Skills, typed tools, and application-owned verification. Odoo ships as a separate
`web-operation-agent-odoo` distribution. The synthetic Contacts application under
[`testapps/`](testapps/contacts/README.md) is internal browser/protocol test apparatus, not a
second public reference.

Important design documents:

- [System design](docs/architecture/system-design.md)
- [Application adapter contract](docs/integrations/application-adapter.md)
- [Durable operations](docs/integrations/durable-operations.md)
- [Security boundary](SECURITY.md)

## Repository layout

```text
src/web_operation_agent/       application-neutral server and browser Runtime
examples/odoo/                 the only public reference integration
  src/web_operation_agent_odoo/  installable adapter
  deploy/reference/              disposable local Odoo fixture
  deploy/single-node/            operated single-node example
  deploy/addons/                 shared Odoo Runtime addon
evals/odoo/                    independent Odoo evaluator and sealed task apparatus
  domain/ fixture/ execution/  evaluation specification and runtime
  provider/ judging/ reporting/ mediation, scoring, and evidence
evals/pageagent/               generic PageAgent comparison bridge
testapps/contacts/             synthetic internal qualification application
frontend/                      assistant panel source
```

For a new application, follow the [Odoo reading guide](examples/odoo/README.md#reading-guide-for-integrators): start at adapter composition and tool specifications, then reads and
writes, identity and routes, Skills, turn composition, and finally deployment.

## Development setup

Python 3.12 and 3.13 are supported. Node.js is required for the complete Runtime and panel
suite.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable .
python -m pip install --no-deps --editable examples/odoo
```

For model-backed local runs, copy the short configuration template and fill in only the
sections you use:

```bash
cp .env.example .env
```

`.env` is ignored by Git and is the local checkout's configuration; `.env.example` contains
no credentials. The Odoo fixture's `provision` command prints the exact Odoo values to use.
Scripted runs need no model settings, and the source and resettable-fixture sections are
optional.

Run the core suite:

```bash
python src/web_operation_agent/tests/run_all.py
```

Run all repository gates:

```bash
scripts/qualify.sh
```

`--require-browser` makes Chromium and built frontend prerequisites mandatory.
`--require-live` additionally requires the configured live Odoo qualification. A `BLOCKED`
gate did not run and is not a pass.

The independent Odoo evaluator's offline apparatus can be run directly:

```bash
PYTHONPATH=src:examples/odoo/src:. \
  python scripts/offline_suite.py --start-dir evals/odoo/tests --top-level .
```

## Odoo reference integration

The adapter provides five domain-specific bounded reads, a generic read fallback, and four
opt-in writes. The domain-specific tools cover customers, products, sales orders, quotations,
and customer invoices. It binds Odoo's HttpOnly browser session to a separately delegated
per-user API key and refuses the composition unless both identify the same database, company,
and actor. Reads report completeness; writes perform application-owned duplicate checks and
readback.

The adapter includes versioned Skills and an optional source compiler for Odoo menus, actions,
views, fields, method documentation, and user-visible refusal conditions. A generic raw-code
index covers allowlisted Python, JavaScript, XML and access declarations alongside those
compiled facts. Both are restricted to modules installed in the running deployment, and
source-backed answers state the conclusion without displaying cited bytes or locations; the
citations remain available to server-side audit. A full matching Git commit can optionally
enable local history; an extracted image tree without `.git` reports history unavailable.
Runtime facts remain authoritative when source or documentation disagrees. The installed
add-on also exposes a bounded actor-scoped model/action catalog. The
Odoo adapter translates it to the core `discover_entities`, `describe_entity`, and
`query_entity_records` contract, then maps validated generic filters back to bounded Odoo
reads under the same delegated actor. Native model names and RPC methods are never Agent
arguments. Generic monetary reads attach the actor-visible currency to each value when
Odoo exposes a valid stored currency binding. The same query can request application-computed, full-scope sums over
matching stored numeric fields; monetary sums are grouped by currency and are independent
of the returned row limit. Count, page and aggregate are separate application reads, not
one transactional snapshot; a disagreement in counts is refused. Without a stored currency
binding an amount remains text and cannot be summed. References to an actor-readable catalog entity retain an opaque related-record
binding for a follow-up read; the related record is not treated as readable until that read
succeeds. This does not authorize writes.
A record found this way carries where it opens (`/odoo/<model>/<id>`, which Odoo 18 opens for any actor who may read the record), and `goto` may use exactly that path. A related record must first be read by its returned opaque entity and exact ID; an unread relation label alone does not authorize opening it. Deterministic navigation reports arrival only after a fresh observation marks the destination rendered and the application reports the matching record identity, not merely when the URL changes.

Provision the disposable local fixture from a checkout:

```bash
PYTHONPATH=src:examples/odoo/src:. \
  python -m web_operation_agent_odoo.deploy provision
```

The private single-node deployment and its short rebuild command live at
[`examples/odoo/deploy/single-node/`](examples/odoo/deploy/single-node/README.md). The rebuild
must update the Agent, run the Odoo addon upgrade, and restart Odoo as one operation:

```bash
./examples/odoo/deploy/single-node/rebuild.sh
```

See the [Odoo integration guide](examples/odoo/README.md) for identity, routes, tool behavior,
source compilation, fixture provisioning, and qualification.

## Known limitations

- Alpha interfaces may still change; compatibility policy is not yet stable.
- Horizontal scaling, active-task migration, and cross-host revocation are not implemented.
- An application must provide its own trusted login assertion, delegated credentials,
  authorization mapping, and application-specific postcondition checks.
- Browser automation cannot provide database transactions or rollback. Uncertain writes need
  application reads and an explicit operator decision.
- The Odoo page guard waits for prior same-session transactions before acknowledging its
  read-only window; a controlled in-flight write test verified this fence. It still cannot
  isolate external effects, other browser sessions, custom mid-request commits, or client
  retries after the window closes. Do not treat it as a general write sandbox.
- Local source search can match Han-language labels and comments, but does not translate a
  Chinese business question into English code identifiers. Cross-language recall still
  depends on the Agent choosing suitable source terms and has not been broadly qualified.
- Source-informed no-Skill form filling has a scripted production-session contract test, but
  no independent model-backed success-rate result yet. It does not establish arbitrary
  page-operation coverage.
- When source lookup is enabled with a hosted model, retrieved allowlisted source is sent in
  model requests. Use that combination only where the deployment's data policy permits it.
- Browser and accessibility coverage is strongest on Chromium; other engines and screen
  readers are not comprehensively qualified.
- A reached page state is checked against the declared path, selected controls and any declared active filters or groups, plus explicit requests for the five conventional views and filter/group changes above. An application may first resolve a reviewed, deployment-bound route alias to its installed path; that does not grant navigation or count as a rendered page. Removing a filter additionally requires that exact filter to have been observed active on the same path earlier in the turn, followed by typed evidence that it is absent. The application must supply typed active-facet state; a same-named menu choice or column never counts. The pending-render signal catches a changed SPA URL with the old page projection; an adapter-reported blocking error and a wrong view also prevent arrival. These guards do not prove that all asynchronous loading has finished or verify record identity, other view names, or that the model declared an implicit facet request such as “look at orders by salesperson.” The new facet action has scripted contract tests, not a repeated model-backed success rate. The earlier 37-task Chinese development run must not be presented as the P8.8 holdout result.
- An empty typed read no longer ends the turn automatically: the Agent receives the exact query result and can check whether the entity and filters match the question. This does not yet prove that a model-selected query covers the user's intent. In the earlier 37-goal run, the old automatic answer incorrectly reported two existing records as absent; one of those reads had searched a customer name for an order number.
- Reaching a declared page target no longer blocks further reading or observation for requests classified as more than pure navigation. Apart from the explicit view and facet checks above, a target still covers only the conditions the Agent declared, so a question or other presentation requirement omitted from that declaration can remain unmet.
- The goal-directed turn scored 35/37 on one rerun of the seen Chinese development set before a write-intent settlement fix; one failure was an unrequested page added to a data answer, and one was a misleading "opened" reply to a create request with no write. The two cases were then rerun three times each: the data question succeeded, and the create request was honestly refused, not fulfilled. A separate 17-task qualification set written and frozen before execution passed twice (34/34 attempts), but its author also implemented the system, so this is not an independently authored holdout or a broad 90% accuracy claim. Odoo and the smaller Contacts REST app implement the index/resolver contract, not equivalent model-level coverage. The server requires every declared deliverable to settle and refuses a page without two stable rendered observations; this does not prove all asynchronous content has loaded. Free-text list search is answered from a scoped read rather than typed into the page's search box. A creation handoff was verified three times on an actor-writable Odoo quotation page, but it only highlights New; it does not prefill or submit, and an actor without create access cannot use it.

The second Chinese goal holdout is frozen with 84 normal goals and 10 honest-limitation goals; see [evaluation details](evals/odoo/README.md). It has offline oracle checks, but has not been model-run or fully walked in a browser. Select it with `--version v5` in the goal runner.

The frozen `v6-rules` set adds 40 source-rule questions bound to deployment release `18.0-20260909`. Its evaluator checks cited source bytes and reports answer and citation matches separately; this is evaluation support, not a completed three-arm experiment. See [the citation contract](evals/odoo/README.md#source-rule-holdout).

## License

See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
