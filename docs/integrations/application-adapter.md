# Integrating an application

An Application Adapter connects one first-party web application to the generic server Agent. The adapter owns application facts and permissions; it does not own the ReAct loop. A new adapter supplies a versioned descriptor, typed business Skills and tools, five executable ports, and a health check. `bind_application()` validates the complete composition before a user task can reach the browser or model.

## Required components

`ApplicationDescriptor` declares the application ID, exact web origin, navigation mode, same-origin PageController module URL, and SHA-256 of the current Skill/tool catalog. Construct the descriptor after the `BusinessToolRuntime`, using `business_tools.catalog_sha256`. A changed Skill or schema then makes a stale deployment fail during composition.

`ApplicationPorts` contains identity, route, data-classification, mutation-postcondition, and reconciliation ports. The data port must classify every typed tool. The postcondition and reconciliation ports must cover every write tool. The identity scopes must equal the non-empty scopes declared by the tool runtime. The runtime and `ApplicationPorts` must use the same route, data, and postcondition objects.

`BusinessToolRuntime` exposes current application data through fixed JSON schemas. Keep credentials and arbitrary HTTP construction inside the application executor. A Skill explains when and how to call these tools; it is not evidence that a current record exists. Each returned route must appear verbatim in the result content and pass the adapter route port before the Agent can use it.

The route port owns every application URL shape used at runtime. Ordinary adapters declare typed-tool paths and return an empty source-contract identity. A source-aware integration may additionally declare a contract route pattern with named `scope` and `route` groups plus explicit application-entry patterns. The generic Agent asks the port to identify routes, discover entry-page scopes, and validate an observed handoff link; it does not parse framework URL conventions. Declare application-specific session handoff query names in `session_handoff_query_keys` and pass that set to `LiveProductSession(sensitive_query_keys=...)` so the embedded Runtime removes those values before DOM content leaves the browser.

## PageController and static Runtime

The application origin must serve an ES module that exports `PageController` and `getElementByIndex`. The minimal example serves a small compatible controller for its synthetic pages. A real integration should pin and deploy the PageController implementation it has qualified, then put the exact same-origin URL in `ApplicationDescriptor.page_controller_module`. A CDN on another origin is rejected.

The Web Operation Agent Runtime is served by `EmbeddedGateway` at `/v1/runtime/embedded.js`; it imports `/v1/runtime/panel_host.js` and the built assistant panel from `/v1/runtime/panel/panel.js` off the same gateway, so the client is always the version the server speaks to. A checkout that has not run `scripts/build_panel.py` has no panel, and the gateway answers that request with `panel_unavailable` rather than serving an older one. Do not copy it into each application or persist a generated bootstrap script. After `LiveProductSession.start()`, obtain `live.bootstrap_script` and inject that ephemeral script directly into the authenticated top-level application page. It contains a short-lived session capability, so it must not be logged, cached, embedded in a static bundle, or exposed to another origin. The public demos use Playwright `context.add_init_script` only as a development host; a production first-party application should inject equivalent session bootstrap from its trusted application shell.

The Runtime starts only in the top-level document and only when `location.origin` matches the session grant. It observes the DOM, executes the fixed generic action set, displays progress and approval UI, and returns receipts. It must not receive model credentials, source indexes, hidden permissions, business plans, or evaluators.

## Identity

An authenticated application should implement `ApplicationIdentityPort` from evidence produced by its trusted server login boundary. Bind the page actor, tenant or workspace, delegated API actor, scopes, delegation ID, issue time, and expiry into a `PrincipalContext`. DOM text, a browser-provided user ID, or possession of a Runtime capability is not identity evidence.

Set `AgentTurnConfig.require_identity=True` whenever the identity port is required. `AgentTurnExecutor(application=...)` validates the principal and exact application origin before it starts the browser transport or model. Every typed tool then receives an `OperationContext` that binds the principal to the session, turn, operation, deadline, and required scope. Synthetic unauthenticated examples must declare `AnonymousIdentityPort` explicitly.

Put on the principal only the scopes the application grants this actor, not every scope the catalog declares. A typed tool whose scope the principal lacks is refused before the application is called, with `BusinessToolPermissionError` (`layer="tool_authorization"`). Map the application's own authorization refusals — an HTTP 403, an access-rule exception — to `BusinessToolPermissionError` too, and keep a rejected or expired credential as `BusinessToolAuthenticationError`. The first is an authorization result the product reports as `permission_denied` and an evaluator scores; the second is a broken apparatus. A refused read or write ends the turn instead of returning to the Agent, which could otherwise look for a page route to the same effect. The scope check governs typed tools only; what the application's own pages show the actor is governed by the application. Scope every read to the actor as well and say so in the evidence origin: a complete search that finds nothing establishes absence only within that scope.

## Writes and verification

Writes are disabled in both the adapter and turn policy by default. A write-enabled adapter must provide a durable mutation journal in production, a postcondition verifier for each write tool, and a reconciliation port. User approval binds one normalized proposal digest, effect, targets, principal, turn, and operation, and can be consumed once.

After one acknowledged dispatch, the postcondition port decides whether the mutation can be recorded as completed. A missing or failed postcondition produces an indeterminate result and no automatic retry. The current conservative reconciler leaves uncertain writes for human handling; persistent reconciliation keys, background recovery, and cross-session in-flight business-key exclusion are later reliability work.

## Transient form handoff

Form prefill is an optional typed-tool result, not another application port and not general browser authority. An adapter may return a `FormDraft` only after it has validated the exact route, semantic field identifiers, control roles and values. The result content must contain those bindings exactly, and the route must also be one of that result's allowed routes. Enable application with `AgentTurnConfig.allow_form_prefill=True`; the model then receives only a no-argument `prefill_form` tool while the one-turn draft exists.

The server consumes the draft once, resolves every field uniquely from a fresh page observation, fills or selects the adapter-issued value, and stops before click or submit. The adapter is asserting that these fields remain transient until the user submits. Do not issue a draft for onchange handlers, autosaving forms, credential fields, uploads, or any control whose edit can persist or trigger an external effect; use the ordinary confirmed mutation path for those cases. Test the claim against the deployed application, including a post-run API or database check that no record was created.

## Composition and qualification

Bind once with `application = bind_application(adapter)`, pass that object to `AgentTurnExecutor(application=application, ...)`, and use its descriptor and route port to configure `LiveProductSession`. Do not pass the raw tool runtime separately in a product composition.

Run the reusable adapter contract assertions for every adapter, then add application tests for valid routes, identity mismatch, read completeness, mutation refusal, approval, postcondition failure, and credential redaction. `scripts/qualify.sh --require-browser` runs the product suite, isolated wheel checks, the minimal embedded Runtime walkthrough, and the canonical browser protocol in real Chromium. A live application gate that reports `BLOCKED` has not run and is not a pass.

Minimal Contacts, Twenty CRM, Odoo and Apache OFBiz currently satisfy the same Adapter v2 contract without application branches in the Agent core. Minimal Contacts is the onboarding reference; the three real application adapters ship as separate distributions.
