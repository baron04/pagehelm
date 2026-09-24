# Integrating an application

An Application Adapter connects one first-party web application to the generic server Agent. The adapter owns application facts and permissions; it does not own the ReAct loop. A new adapter supplies a versioned descriptor, typed business Skills and tools, executable ports, and a health check. `bind_application()` validates the complete composition before a user task can reach the browser or model.

## Required components

`ApplicationDescriptor` declares the application ID, exact web origin, navigation mode, same-origin PageController module URL, and SHA-256 of the current Skill/tool catalog. Construct the descriptor after the `BusinessToolRuntime`, using `business_tools.catalog_sha256`. A changed Skill or schema then makes a stale deployment fail during composition.

`ApplicationPorts` contains identity, route, optional current-form, data-classification, mutation-postcondition, and reconciliation ports. The data port must classify every typed tool. The postcondition and reconciliation ports must cover every write tool. The identity scopes must equal the union of non-empty tool-runtime scopes and form-port scopes. The runtime and `ApplicationPorts` must use the same route, data, and postcondition objects.

`BusinessToolRuntime` exposes current application data through fixed JSON schemas. Keep credentials and arbitrary HTTP construction inside the application executor. A Skill explains when and how to call these tools; it is not evidence that a current record exists. Each returned route must appear verbatim in the result content and pass the adapter route port before the Agent can use it.

For broad read coverage, implement the optional application-neutral `EntityReadPort` and
compose it with `EntityReadTools`. The Agent-facing surface is always
`discover_entities`, `describe_entity`, and `query_entity_records`. The adapter translates
its native ORM model, REST collection, OpenAPI resource, GraphQL type, or other metadata into
`EntityCatalog`, `EntityDescriptor`, `EntityField`, and `EntityQueryPage`. Native resource
names remain adapter-private references; the core mints actor/catalog-bound opaque entity and
field IDs. Catalog or field-descriptor drift invalidates the old IDs. The generic filter is
AND-only, bounded, uses fixed operators, and has server-fixed ordering, page size, and offset;
it is not an arbitrary method, URL, SQL, domain, or request-body escape hatch.

An adapter must run `assert_entity_read_contract` when it enables this surface, plus its own
authorization and translation tests. This repository runs that same black-box assertion over
the Odoo ORM/RPC translation and the structurally different synthetic Contacts REST/list
translation. Passing only one adapter is not evidence that an Agent capability is generic.

A read tool's `QueryEvidence` decides what an answer may say about the rows it returned. Set `matched_count` to the number of matching records in scope, and `count_exact=True` only when the application counted them all, so a list limited to 20 rows of 72 says "72 in total, 20 listed below". For a search by name, set `looked_for` to the searched text and `exact_matches` to how many matches carry exactly that name; leave it `None` when the application cannot tell. Only exact matches let an answer say that a record of that name exists.

Evidence names fields and scopes the way the tools do, and that is what the verifier checks. To show them in the reader's language, pass `answer_labels={"zh": {...}}` to `BusinessToolRuntime`, keyed by the exact field names and scope strings the evidence carries, and by `field=value` for coded values such as an order state. Labels change only how an answer is rendered, never what it claims; the answer basis keeps the original terms.

The route port owns every application URL shape used at runtime. Ordinary adapters declare typed-tool paths and return an empty source-contract identity. A source-aware integration may additionally declare a contract route pattern with named `scope` and `route` groups plus explicit application-entry patterns. The generic Agent asks the port to identify routes, discover entry-page scopes, and validate an observed handoff link; it does not parse framework URL conventions. Declare application-specific session handoff query names in `session_handoff_query_keys` and pass that set to `LiveProductSession(sensitive_query_keys=...)` so the embedded Runtime removes those values before DOM content leaves the browser.

## PageController and static Runtime

The application origin must serve an ES module that exports `PageController` and `getElementByIndex`. The minimal example serves a small compatible controller for its synthetic pages. A real integration should pin and deploy the PageController implementation it has qualified, then put the exact same-origin URL in `ApplicationDescriptor.page_controller_module`. A CDN on another origin is rejected.

A host page includes one script to have the assistant: `assistant.js`, served by `EmbeddedGateway` at `/v1/runtime/assistant.js`. It carries the boot supervisor, the Runtime and the assistant panel in one file, and nothing is fetched after it; served by the same gateway, the client is always the version the server speaks to. It is a build artifact: a checkout that has not run `scripts/build_runtime.py` gets `runtime_unavailable` from the gateway rather than an older copy. Do not copy it into each application. A production host declares `<script type="module" src=".../v1/runtime/assistant.js" data-woa-bootstrap="/your/bootstrap"></script>` on its signed-in pages. The bootstrap endpoint, on the application's own origin, turns the signed-in application session into an agent session: it receives `{"runtime_instance_id": ...}` and answers `{"schema_version": 1, "config": ..., "renew_at": ...}` built with `EmbeddedGateway.bootstrap_config`. An application that issues identity separately adds `data-woa-identity`: the script posts `{"runtime_instance_id": ...}` there first and hands the answer to the bootstrap endpoint, which is what the Odoo addon does. The panel's launcher is on the page from the moment the script runs, but no session is made until one is needed: when the person sends something, when they open a conversation the tab already has, or -- without anyone asking -- when a turn is under way, as on a page the agent itself opened. A session with nothing to do is let go after five minutes and the next message makes a new one; the conversation is the server's and comes back with it. From then on the script renews and, while a turn depends on it, reconnects sessions itself. When a session cannot be started, the panel says why and offers Retry; nothing is retried behind the person's back unless a turn is under way. A tab's progress is kept in its `sessionStorage` (`woa.assistant.engagement.v1`). For development hosts, `live.bootstrap_script` returns an ephemeral script that hands the page a whole session and loads the same `assistant.js`; it contains a short-lived session capability, so it must not be logged, cached, embedded in a static bundle, or exposed to another origin. The public demos inject it with Playwright `context.add_init_script`.

The Runtime starts only in the top-level document and only when `location.origin` matches the session grant. It observes the DOM, executes the fixed generic action set, displays progress and approval UI, and returns receipts. It must not receive model credentials, source indexes, hidden permissions, business plans, or evaluators.

## Identity

An authenticated application should implement `ApplicationIdentityPort` from evidence produced by its trusted server login boundary. Bind the page actor, tenant or workspace, delegated API actor, scopes, delegation ID, issue time, and expiry into a `PrincipalContext`. DOM text, a browser-provided user ID, or possession of a Runtime capability is not identity evidence.

Set `AgentTurnConfig.require_identity=True` whenever the identity port is required. `AgentTurnExecutor(application=...)` validates the principal and exact application origin before it starts the browser transport or model. Every typed tool then receives an `OperationContext` that binds the principal to the session, turn, operation, deadline, and required scope. Synthetic unauthenticated examples must declare `AnonymousIdentityPort` explicitly.

Report the actor's own time zone from `time_zone()` on the executor when the application keeps one, and pass it to the turn as `AgentTurnConfig.time_zone`. Relative dates -- today, this month -- are then the person's days and every reported time matches what the application's own screens show them; without it the turn says nothing about the clock and the model falls back to UTC timestamps.

Put on the principal only the scopes the application grants this actor, not every scope the catalog declares. A typed tool whose scope the principal lacks is refused before the application is called, with `BusinessToolPermissionError` (`layer="tool_authorization"`). Map the application's own authorization refusals — an HTTP 403, an access-rule exception — to `BusinessToolPermissionError` too, and keep a rejected or expired credential as `BusinessToolAuthenticationError`. For a mutation, use `BusinessToolPermissionError` only when the application guarantees the refused call made no business change; use `BusinessMutationIndeterminate` if a partial or unknown effect remains possible. The first is an authorization result the product reports as `permission_denied` and an evaluator scores; the second is a broken apparatus. A refused write ends the turn. A refused read is returned to the Agent, which may then answer from what it already read and must say which part it could not do; the same refusal closes the page as a source of facts for the rest of that turn, so the Agent cannot take the refused records off the screen instead. The scope check governs typed tools only; what the application's own pages show the actor is governed by the application. Scope every read to the actor as well and say so in the evidence origin: a complete search that finds nothing establishes absence only within that scope.

## Writes and verification

Writes are disabled in both the adapter and turn policy by default. A write-enabled adapter must provide a durable mutation journal in production, a postcondition verifier for each write tool, and a reconciliation port. User approval binds one normalized proposal digest, effect, targets, principal, turn, and operation, and can be consumed once.

After one acknowledged dispatch, the postcondition port decides whether the mutation can be recorded as completed. A missing or failed postcondition produces an indeterminate result and no automatic retry. The current conservative reconciler leaves uncertain writes for human handling; persistent reconciliation keys, background recovery, and cross-session in-flight business-key exclusion are later reliability work.

## Transient form handoff

Form prefill is optional and is not general browser authority. A dedicated typed tool may
still return a `FormDraft` for a specialized workflow. For a no-Skill fallback, implement
`ApplicationFormPort`: `describe_form` returns actor-scoped writable field metadata for the
current route, and `prepare_form` validates selected values and returns the exact draft.
Enable it with `AgentTurnConfig.allow_form_prefill=True`.

The generic `describe_form` tool intersects the adapter descriptor with the latest viewport
observation; metadata-only, DOM-only, duplicated and credential-shaped fields disappear. It
returns short-lived opaque form and field IDs, not native field names or selectors.
`prepare_form` re-fetches both sources, rejects stale IDs and values that do not match the
portable field type or declared choice, and refuses a draft that adds fields or changes the
route or persistence contract. The no-argument `prefill_form` appears only while that exact
server-retained draft exists. Form scopes are checked before adapter metadata is read, and an
actor missing them does not receive the generic form tools.

The server consumes the draft once, resolves every field uniquely from a fresh page observation, fills or selects the adapter-issued value, and stops before click or submit. The adapter is asserting that editing these fields persists nothing by itself. Do not issue a draft for onchange handlers that write, forms that save as they are edited, credential fields, uploads, or any control whose edit can persist or trigger an external effect; use the ordinary confirmed mutation path for those cases. A form the application saves when its page is left (Odoo 18 does) is acceptable only because the assistant asks before it leaves any page and the prefill answer tells the user that leaving may save or discard the input. Test the claim against the deployed application, including a post-run API or database check that no record was created.

Source lookup can explain a field, validation rule, or workflow before preparation and may
inform the value selected for an existing field. A source-derived value includes a turn-local
reference and exact support span; the server checks that provenance and removes it before
calling the adapter. This check does not prove the model interpreted the rule correctly.
Source cannot create a form ID, field ID, selector, choice, scope, or write permission. The
current page and current application metadata are the execution authorities. When adopting
the generic surface, run the shared `assert_current_form_contract` against this adapter and
a real browser gate; an adapter-only unit result does not establish end-to-end form accuracy.

The Odoo reference translation has no global enable switch, but defaults to the exact
fields covered by three standard-module browser qualifications. Its `fields_get` metadata
cannot by itself prove that a custom `onchange` handler has no persistent or external
effect; an unknown field or page is not a prefill draft until separately qualified. A
save-on-leave edit draft requires effect-and-target confirmation before input.

## Composition and qualification

Bind once with `application = bind_application(adapter)`, pass that object to `AgentTurnExecutor(application=application, ...)`, and use its descriptor and route port to configure `LiveProductSession`. Do not pass the raw tool runtime separately in a product composition.

Run the reusable adapter contract assertions for every adapter, then add application tests for valid routes, identity mismatch, read completeness, mutation refusal, approval, postcondition failure, and credential redaction. `scripts/qualify.sh --require-browser` runs the product suite, isolated wheel checks, the minimal embedded Runtime walkthrough, and the canonical browser protocol in real Chromium. A live application gate that reports `BLOCKED` has not run and is not a pass.

Odoo is the only public reference implementation. Its installable distribution satisfies
the complete Adapter v2 contract without application branches in the Agent core. The small
Contacts application under `testapps/` remains internal browser and protocol test apparatus.
