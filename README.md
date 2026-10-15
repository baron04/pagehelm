# Web Operation Agent

[简体中文](README.zh-CN.md)

Web Operation Agent is an alpha framework for embedding an AI assistant in a first-party web application. Planning, model access, credentials, policy, approvals, and audit state stay on the server. A small browser Runtime observes the current page, executes fixed generic actions, displays progress and confirmation UI, and returns receipts.

The repository has one public reference implementation: [Odoo 18](examples/odoo/README.md). It is not a hosted service, a general browser extension, or a production-ready autonomous agent.

## Current capabilities

- A goal-directed turn asks the model to declare the request against the application's index (entities, fields, pages, views, filters, groups, sections and controls, compiled by the adapter under the acting user). The server then resolves each declaration deterministically: records by the application's own name search, field values by typed reads, counts, sums and numeric extremes by scoped queries, pages by reviewed routes, tabs by their visible name, filters and groups by the page's own state, and a requested change by highlighting its control for the person. The reply is rendered from resolved values, never from model prose, so a fact the turn reports is one it read. A part that could not be done is told as such; a turn is marked complete only when every declared part settles. When several records fit and nothing tells them apart, the turn asks which one. A named record that does not exist is never turned into a creation. Conditions take comparison operators on numbers, dates and text; dates accept a day, month, year, range or named period in the user's time zone. The declaration is model output and can name the wrong entity, field or scope; this path adds model calls and latency, and is not a one-call turn.
- A goal turn can prefill a form and hand it over. It enters the values the person gave, checks what each field then shows, highlights the submit button, and never submits; a value that cannot be entered with certainty is left to the person and said so. Form prefill is off unless the application installs a reviewed form port and sets `allow_form_prefill=True`. Page filtering through the page's own query controls is likewise opt-in per application, and counts as done only after the application reports the applied query.
- With a compiled source map attached, the index can carry rules of the deployment's source (when a control is shown or disabled, when a field is required or read-only, what a method refuses), and the reply cites the exact source lines. An adapter may also carry its organisation's reviewed conventions for what some words mean there.
- A server-side ReAct loop receives every tool result and a fresh page observation before its next decision, with step, time, token, and provider-spend limits. Integrations can opt into actor-bound data reads (`AgentTurnConfig.data_reads`), application-issued action effects (`action_policy`), rendered-route proof for navigation (`require_page_route`), and an up-front task declaration (`require_task_contract`). All are off by default, and none grants write authority.
- Application-owned Skills use `SKILL.md` (YAML metadata and Markdown instructions) with bounded, progressively loaded references. The goal planner offers resource reads only for readable text attachments in loaded packages; the instructions are already loaded. Skills describe business knowledge, queries and page or write workflows; registered typed tools define actual capabilities, actor scopes and effects independently. Current facts come from application reads. When explicitly enabled, deployment-bound source lookup can answer source questions with server-side provenance. Source retrieval is optional and grants no authority.
- The step Agent can search or page through bounded, redacted evidence from the current page observation. This does not search application data or establish absence.
- An application-neutral entity-read contract can discover the current actor's readable business entities and perform bounded reads without adding one Skill per entity. A separate record-reference read locates name or number candidates and returns ambiguous matches rather than choosing one. Odoo and the structurally different synthetic Contacts REST adapter run the same contract tests.
- A separate `find_page_entries` read ranks the current actor's page and action metadata. It returns a route only for an adapter-reviewed, standalone destination. This does not make every application page operable.
- A read-only `resolve_goal_candidates` call can combine up to three answer, page or handoff candidate lookups. It neither chooses a candidate nor completes a goal.
- An optional application-neutral current-form contract supports a no-Skill fallback for safe scalar form filling (`describe_form`, `prepare_form`, `prefill_form`). It types a draft, checks the live field values, and never clicks the submit control. Odoo's standard create forms save automatically on leaving, so generic manual-submit prefill is refused there; its typed customer creation and edit handoffs require an enabled write-confirmation channel and a fresh approval.
- Turn context is assembled by source role: the current page is the live baseline; bounded conversation history is included for follow-ups; Skills are selected from the current task; deployment source is a lazy cited lookup rather than injected wholesale.
- Browser actions are checked against the current observation and application route policy. Page observations are viewport-bound and expose at most 200 live controls. Working the page needs the person's leave, given once per conversation on a visible card at the first page operation (opening a page, clicking, filling, choosing, submitting, going back, applying a filter); later operations and later turns of the same conversation go ahead on it, and a new conversation or a declined card asks again; the tool-loop turn also asks again after it revises its reading of the task. The leave lives in the host's session and is not restored after a restart. Reading, scrolling, waiting and highlighting ask nothing. The leave is not a write approval: each business change is still confirmed on its own card.
- For a page operation the assistant cannot perform, `highlight` can mark one named, currently visible control and end the turn without clicking or submitting. This is a guided handoff, not a claim that the business change happened.
- A control that sits in a collapsed menu (a record's "Delete" under its "Actions" menu) is found the same way in every application: the goal turn opens up to three menus the page itself declares collapsed, marks the control, and names the menu in the reply. Nothing in a menu is chosen.
- For a request that names a page state (open a screen, switch a tab, apply a filter or grouping, arrive at a record), the Agent declares that state up front with `set_target`, and the server compares each fresh observation with the declaration before reporting arrival. A declared target grants no permission. The sentence the user reads is composed from the observed page, never from model prose.
- Generic click can stay disabled as an action while an application re-allows part of it through an optional click policy. The Odoo reference integration allows tabs, the Home button, links to reviewed pages and records, and named view-switch, pager, search and menu controls on those pages. Workflow buttons, New, Actions, row selection, inline editing and unreviewed pages remain refused.
- An integration with a page write guard may expose up to 32 application-owned filter and group choices; the optional `apply_facet` tool applies one, and a fresh observation of the applied state settles it.
- An application can make the signed-in session read-only while the Agent acts on the page by declaring a `page_write_guard` path. Odoo's `woa_runtime` 1.5 implements it: while the window is open, every request of that session runs in a read-only transaction. Deploy the 1.5 addon before this adapter.
- Registered typed reads do not require a matching Skill. A package can declare reviewed entity and page identities in `agents/woa.toml` for knowledge selection, independently of installed write tools. Integrations may require writes to bind an explicitly loaded Skill and its reviewed operation list. This list limits proposals; it neither installs tools nor grants permission. Tool schemas are filtered by deployment write enablement and current actor scope, then checked again at dispatch.
- With business writes enabled and effect-contracted Skills installed, an ordinary goal may consult execution once before a change, refusal or source-only exit. A proposal can yield back before approval when execution is not requested or no installed operation fits. After an invalid scope declaration, yielding resumes the ordinary full-index recovery without source lookup; a source-enabled deployment retains its scope-dependency refusal. Normal query/page decisions keep their existing path; source-only requests can incur an extra selection call. Write-disabled deployments use the ordinary interpreter with optional Skill knowledge. This admission mechanism has synthetic contract coverage; it does not establish model success rates. On the measured Token Plan DeepSeek endpoint, required declarations use provider `auto` while the client still rejects a response without tool calls.
- Only the person's request can ask for a write. The Skill task declaration interprets execution intent from the complete request; a routing hint or tool availability does not prove it. Interpretation can be wrong, including negation. Record and page text grant no permission, and every write still requires a complete visible approval card.
- Writes are disabled by default. Core turn configuration initially disables every browser action that can initiate an application write, including navigation; an adapter must explicitly opt into its reviewed subset. An enabled write binds its exact effect and targets to a fresh one-use approval, dispatches at most once, and is verified through the application. A timed-out or otherwise uncertain write is recorded as `indeterminate` and never retried automatically.
- A short-lived actor/tenant/workspace principal can be bound to the browser capability, typed-tool scopes, approvals, mutations, audit records, and revocation state.
- The React assistant panel is a view over server state. Its launcher is on the page from the moment the script runs. A session is made when the person sends something or reopens a conversation, and an idle one is let go after five minutes; a session that lapsed comes back with the next message, and a start that failed is said in the panel, with Retry. Opening the panel restores the current actor's server history.
- One session can be open on several pages at once. Every page follows the same conversation; a turn runs on the page that asked it, and only that page is handed its page commands.
- The panel opens as a floating window on the launcher's corner, or as a full-height drawer at the right edge; a title-bar button switches between them. On a viewport at least 900px wide the drawer narrows the host page; elements the application positions against the viewport (dialogs, notifications) are not narrowed and can sit partly under the drawer. The chosen mode, placement and width are kept in the browser's local storage.
- Optional SQLite stores provide session ownership, progress outbox, bounded conversation context, mutation journals, backups, and explicit operator settlement.

These are implemented-path claims, not a general success-rate claim. The repository has no public benchmark large enough to establish superiority over another browser agent.

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

The core package is application-neutral. Application behavior enters only through adapters, Skills, typed tools, and application-owned verification. A goal-directed turn is resolved by the core over the adapter's goal data (its index, schema and reads); the adapter never resolves a declaration itself. Odoo ships as a separate `web-operation-agent-odoo` distribution. The synthetic Contacts application under [`testapps/`](testapps/contacts/README.md) is internal browser/protocol test apparatus, not a second public reference. The experimental [Yudao adapter and resettable deployment](examples/yudao/deploy/README.md) provide another goal-data implementation; they are not a production readiness claim.

The Yudao native host uses only `slim`, using the application-neutral `ModelDrivenTurnExecutor`. Startup requires a release-matched v2 method fence, `--native-page-policy` with reviewed routes and handlers, live forms and the application’s own read-only window. Missing or incompatible configuration fails before model or network startup; there is no fallback. Old `goal` and `page-agent` configurations are rejected with migration diagnostics; their construction branches are removed. Writes stay disabled by default and browser submission is always disabled. A page the assistant opens is opened by the application's own Vue Router in the document the person is on, built fresh and with the router's history intact, so the assistant's window stays open and the application does not start again; an address the application cannot open that way, or the one already shown, is loaded as a document. The slim turn keeps the goal turn's protocol (identity, the conversation's visible leave to work the page, the write watch, receipts and budget) and lets the model choose each step from a small fixed tool set: the actor's own menu, the application's read-only API called as the actor, and the Runtime's click, fill, select, scroll, navigation and highlight. It starts only under the method fence with the application's own read-only window and never presses a submit control; its API tool does not call a GET the method fence would refuse. Without `--native-writes` it performs no writes. With it, the same conversation may load an installed Skill (`load_skill`) and propose one of that Skill's typed operations, which then becomes a tool with its installed input schema. Each proposal goes through the shared write service: read-only preparation under the actor, registered literal inputs matched against the person's own messages, the application's projection of the prepared effect compared with the proposed inputs, one complete visible approval, one dispatch and actor-bound readback. A refusal before the person is asked returns to the same conversation for a bounded number of corrections; a declined, refused or uncertain write ends the turn and is never sent again. The Skill-selection, single-proposal and yield steps of the goal executor are not used by this turn. Across turns it keeps only the host's bounded conversation state — the person's words, each turn's answer and the writes that turn verified, four turns and 32 KiB by default, persisted under the session's identifier — so a conversation continues after the host restarts; what an earlier turn read or saw on the page is read again. What an API read or a lookup by name tells the model has its credentials blanked first — the fields this adapter never reads (passwords, secrets, keys, tokens, channel configuration), a named setting whose name says it is one, and credential or session assignments inside any string — and its rows, values and names are fenced as application data, which the model is told to read as facts and never as instructions; business fields the signed-in person may read in the application are not masked. While a turn runs, the panel's timeline says what it is doing between its page actions: which page, data source or record it is looking for, that it is reading business data and how many rows of which list came back, that a change is being prepared, and that the result is being checked; the sentences are the server's. The host's default `--warm-index` reads an actor's menu and builds their application index in the background when their session opens, so those reads can overlap the wait before a turn. This warms menu/API descriptions and the outline without prefetching line-table details. Read volume depends on installed modules. Use `--no-warm-index` for an explicitly cold session; warming does not measure cold-start performance. This has been run only on small synthetic evaluation sets.

The common model loop validates closed tool schemas, returns each typed result and fresh page observation to the same Agent, and uses application-issued, single-use action or transient-draft permits. Yudao owns its menu/API knowledge and date, paging and line procedures; a synthetic Contacts REST/HTML application exercises the same core. An ordinary model finish is `not_claimed`: local action readback does not independently verify the whole request or model answer. When deployment source is configured, the same loop offers allowlisted source search, bounded line reads and optional Git history. Source conclusions cite current-turn evidence and literal support, using the existing server answer renderer. These checks establish attribution, not semantic correctness. A source-only turn can finish without a browser observation or business metadata; an application tool must first bind a real observation from the submitting page. Ordinary operation requires no source repository.

Yudao query selections bind the current live option label and native scalar value, then verify the successful applied query. Only explicitly reviewed query-form reveal buttons can expose additional fields. Typed data permission denials stop the turn; structural API corrections name installed fields and never echo rejected input.

Slim record navigation checks the actor's entity/page outline and reads the exact detail before and after the action, without compiling unrelated embedded-line metadata. For transient form choices, slim calls the Runtime’s bounded, owned-list selection directly: it opens the list, searches only for the exact label, and refuses ambiguous or disabled choices. A missing label returns bounded, redacted labels from that list without substituting a name. If the expanded input has no unique current controller ref, dismissal is skipped; unchanged-draft and same-form checks still apply. It is recoverable only when the Runtime proves no option was selected and the local draft stayed unchanged, and an optional application proof confirms the same form and actor; other acknowledged draft failures end the turn. Reviewed form entry can be combined with navigation (`goto(open=...)`). A named-field batch can carry `then_highlight` and `answer`: every entry is still admitted and verified separately, then the server checks supplied and previously observed untouched fields and the existing Runtime form handoff checks the live supplied values and marks the submit button. The batch may first add 1–4 lines through an exact reviewed button (`add_lines`); each addition must retain the live form and previous values. Unnamed line controls are checked by their exact current ref and live form instance. For reviewed ERP sales-order and stock-in drafts, a product’s automatically copied price is checked against an actor-bound exact product read; other untouched values must remain unchanged. A complete filter batch may carry `then_click` and `answer` and end only on that tool’s actual application-verified query receipt. A missing or failed field prevents the search click. Incomplete, replaced or offscreen fields require further observation. No browser submission or write approval changes. Duplicate label spans inside a button or link are hidden from the model’s numbered controls while the parent keeps its current name. Customer detail navigation reports edit eligibility from the acting user’s live application permission rows, independently of whether an edit control is visible. Optional name previews use only declared query parameters and skip endpoints needing additional inputs. Column-projected API results name documented, returned columns omitted from the projection so the model can request them. Turn receipts separate action approval, admission, dispatch and verification time; action refusal audits retain control structure and a label digest, without field values or raw labels. Immutable action outcomes preserve refusal/error codes and dispatch state through later waits. A refused action does not trigger settlement waits; existing verification observations can count toward settlement. Within one Yudao slim turn, reads share a successful actor/tenant/workspace match for the same principal, session, turn and credential generation. Each GET still authenticates and authorizes at the application; cancellation, expiry and credential changes are checked before and after reads. Permissions are read afresh for customer edit facts and existing form/write admission. A credential change refuses the current turn; the identity match is discarded at turn end.

Within a slim turn, the same click, input, selection, scroll or field-fill with the same arguments is refused if it has already run from that page state; a fourth repeat ends the turn as incomplete. Observation counters and temporary markers for newly seen controls do not count as progress. A fresh successful list read still counts as a change, so repeated searches can run. Three unreviewed refusals of the same uniquely identifiable control end the turn, even when unrelated searches intervene; every attempt still goes through admission, and an ambiguous target is not merged with another. A scroll that leaves the page observation unchanged tells the model that the screen did not change.

The experimental Skill runtime adds task knowledge to the existing query/page interpreter. For applications installing `ToolDefinition.effect_contract`, the default `complete_request_check="single"` directly proposes installed operations from the complete request and locks the application's projection of their effects. Scope selects knowledge and tools; its inferred categories do not require additional proposal steps. The same Agent can call registered actor-authorized reads and loaded knowledge resources, and receives their results before its next decision. When a reply also contains proposals or completion, only the reads run; those proposals must be made again after observing the results. Earlier accepted proposals and the existing read/correction limits remain unchanged. The proposal requires a tool response and explicit completion. Before the first write approval, the server checks schemas, literal sources, dependencies, declared goal capabilities and independently preparable effects. Schema, source and read-only preparation errors share one correction budget; accepted steps remain recorded. Preparation correction can read current facts before replacing the failed operation. An empty completion is invalid; an exhausted validation error retains its failure state and cannot implicitly yield to the ordinary path. Explicit preservation uses application-registered field identities; ordinary patch preservation belongs to the application. These checks do not independently detect a swapped field or a requirement omitted from the proposal.

With business mutations enabled and installed effect-contract Skills, the existing scope call also offers select_skills. Execution requests select one to four installed identities by their descriptions, without declaring business values or steps. One selection and one scope may arrive together; both must validate, and scope contributes lookup hints rather than an additional plan. Ordinary queries, pages, preparation and source questions retain declare_scope and its existing parser. The full original request reaches the native proposal Agent, so selection does not assert complete understanding or write intent. Invalid routing gets one structural correction before preparation. Write-disabled integrations retain their original messages, schemas and recovery path. Legacy operations without effect contracts retain their existing admission.

The optional `complete_request_check="anchored"` reads the request independently in parallel with the draft, in the same server Agent and budget. It declares actions, targets and supplied facts without field names, and compares them with the application's projected draft plus separate read/page obligations. A disagreement permits one whole-task reconciliation before preparation. Both readings use loaded Skill business knowledge; the intent reader does not see tool schemas or the draft. Agreement on values does not establish agreement on field identities or general reliability.

Skill planning reads use their published JSON Schema for structural checks, including opaque ID patterns and dynamic filter values. Schema references resolve locally; the application retains current identity, field and permission checks.

The shared entity-read surface offers lookup by description first, then binds entity and field arguments to IDs returned to the same Agent. A field query requires a described entity and accepts its returned field IDs. This narrows Skill planning; ordinary reads and application authorization retain their existing contracts.

Native proposals retain the planning-only envelope `{"request_intent": "execute", "inputs": {...}}` and exact user-text binding for registered literal fields. Explicit preservation belongs at the envelope's top level. In the single-proposal path, declare_goal describes reads, pages and form preparation with write_intent false; execution declarations are rejected through the shared correction budget while earlier accepted operations remain recorded. Application preparation projects the actual semantic effect, resolved identities, preserved inputs, defaults and compound effects. The projection is bound to the approval digest and checked against the locked request before approval. Application receipts must cover every required effect before the mutation journal records completion; incomplete acknowledged effects remain indeterminate and are never retried. Ordinary non-Skill queries/pages/source answers keep their existing interpreter. Integrations without an effect contract retain their existing declaration path.

Yudao's thirty native write operations are organized into twenty-four goal-scoped Skill packages: customer creation and contact creation under an existing customer, customer profile update, contact profile update, pool release, receive, lock and deal status; clue maintenance, compound follow-up and receivable planning; ERP supplier/warehouse/customer/product records; system department/post/notice; mall brand/category; member tag/group and workflow category. Reference material several goals genuinely share — disambiguation principles, hierarchy and unit conventions, entity distinctions — is maintained once in the application's assets and assembled by the application into each package's controlled references as a single source with progressive loading; completion conditions stay in the body text. Actor-read relation names and hierarchy paths bind foreign keys; ERP prices remain yuan and weights kg. System notices create records without pushing messages. Dedicated customer pool/state transitions, receivable plans and OA publication expose and verify their compound effects; OA publication uses the application’s dynamic subordinate rule, not a fixed recipient list. Native owner-time/history metadata that the API does not expose is not independently verified. Yudao and the synthetic Contacts REST integration implement the effect contract. Creating definition records does not assign users, members or permissions, or deploy/start a workflow. Enable Yudao writes explicitly with `native_writes=True` or `--native-writes`, through the native host’s sole model-driven loop; a supporting Skill, full visible approval and actor-bound readback remain required. Browser submission remains disabled. Current dictionary choices constrain proposals and are checked at preparation and dispatch. Static explanations use exact loaded passages. Contract checks establish these behaviors, not model accuracy or speed.

Yudao follow-up effects depend on the main entity: customer/clue update recent time, next time, recent content and the followed flag; contact omits the flag, business omits recent content, and contract updates only recent time. One application definition supplies approval text, effect projection and readback requirements. Required fields must be readable before approval and dispatch; an incomplete acknowledged effect remains indeterminate.

Scope retrieval uses actor-visible entity labels before scanning Latin name fragments across the application. When the request names an entity and contains no document code, names are located on the declared entities and their links after the scope decision. Document codes and requests without entity labels retain global reference lookup. Deferred lookup establishes neither record absence nor permission; ordinary queries retain their independent readings and repairs.

Source questions about a page remain knowledge requests. A source-enabled execution request may select Skills and declare its source questions in the same routing call. The existing source scope preserves live-read dependencies for current comparisons. A plain execution request needs no source slots. Every requested read, page or source outcome remains in the complete request; Skill selection grants no route, data or write authority.

Skill write planning also offers actor-read candidates for scoped names that were not uniquely located, preserving complete display names. Compound operations include the reviewed entity scopes of the tools currently offered to the actor, so a related record can be located even when the preliminary scope names only the operation's main entity. Initial candidates are not a complete lookup. In the single path, a typed target-reference preparation error can use the shared correction and registered reads; unresolved ambiguity returns current choices through the existing clarification UI before any write. A reply starts a fresh declaration and approval; dispatched or uncertain writes are never retried. Permission failures never enter model correction, and missing records without clarification choices retain their refusal state.

Important design documents:

- [System design](docs/architecture/system-design.md)
- [Application adapter contract](docs/integrations/application-adapter.md)
- [Skills and typed operations](docs/integrations/skills.md)
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

For a new application, follow the [Odoo reading guide](examples/odoo/README.md#reading-guide-for-integrators): start at adapter composition and tool specifications, then reads and writes, identity and routes, Skills, turn composition, and finally deployment.

## Development setup

Python 3.12 and 3.13 are supported. Node.js is required for the complete Runtime and panel suite.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable .
python -m pip install --no-deps --editable examples/odoo
```

For model-backed local runs, copy the short configuration template and fill in only the sections you use:

```bash
cp .env.example .env
```

`.env` is ignored by Git and is the local checkout's configuration; `.env.example` contains no credentials. The Odoo fixture's `provision` command prints the exact Odoo values to use. Scripted runs need no model settings, and the source and resettable-fixture sections are optional.

The selected model comes from `AGENT_OPENAI_MODEL`. Changes require restarting a source-installed server, or rebuilding and redeploying a wheel/container installation.

Build the browser assets. The assistant script the gateway serves and the trace replay page are generated, not committed; a checkout without them has no assistant in the browser, and the browser-backed tests and the one replay-page test fail or skip until they are built:

```bash
python scripts/build_runtime.py --install
python scripts/build_panel.py
```

`--install` runs `npm ci` once for a fresh checkout. Rebuild after pulling frontend changes; a separate worktree needs its own build.

Run the core suite:

```bash
python src/web_operation_agent/tests/run_all.py
```

Run all repository gates:

```bash
scripts/qualify.sh
```

`--require-browser` makes Chromium and built frontend prerequisites mandatory. `--require-live` additionally requires the configured live Odoo qualification. The offline gates include the Yudao adapter, native host lifecycle, evaluation apparatus and separate wheel import/assets checks. A `BLOCKED` gate did not run and is not a pass.

The independent Odoo evaluator's offline apparatus can be run directly:

```bash
PYTHONPATH=src:examples/odoo/src:. \
  python scripts/offline_suite.py --start-dir evals/odoo/tests --top-level .
```

## Odoo reference integration

The adapter provides five domain-specific bounded reads, a generic read fallback, and four opt-in writes. The domain-specific tools cover customers, products, sales orders, quotations, and customer invoices. It binds Odoo's HttpOnly browser session to a separately delegated per-user API key and refuses the composition unless both identify the same database, company, and actor. Reads report completeness; writes perform application-owned duplicate checks and readback.

The adapter includes standard Markdown business workflows (statically adapted; not requalified on Odoo) and an optional source compiler for Odoo menus, actions, views, fields, method documentation, and user-visible refusal conditions. Both the compiled facts and a generic raw-code index are restricted to modules installed in the running deployment. Runtime facts remain authoritative when source or documentation disagrees. The installed add-on also exposes a bounded actor-scoped model/action catalog, which the adapter translates to the core entity-read contract; native model names and RPC methods are never Agent arguments.

Provision the disposable local fixture from a checkout:

```bash
PYTHONPATH=src:examples/odoo/src:. \
  python -m web_operation_agent_odoo.deploy provision
```

The private single-node deployment and its short rebuild command live at [`examples/odoo/deploy/single-node/`](examples/odoo/deploy/single-node/README.md). The rebuild must update the Agent, run the Odoo addon upgrade, and restart Odoo as one operation:

```bash
./examples/odoo/deploy/single-node/rebuild.sh
```

See the [Odoo integration guide](examples/odoo/README.md) for identity, routes, tool behavior, source compilation, fixture provisioning, and qualification.

## Known limitations

- Alpha interfaces may still change; compatibility policy is not yet stable.
- Horizontal scaling, active-task migration, and cross-host revocation are not implemented.
- An application must provide its own trusted login assertion, delegated credentials, authorization mapping, and application-specific postcondition checks.
- Browser automation cannot provide database transactions or rollback. Uncertain writes need application reads and an explicit operator decision.
- The Odoo page guard cannot isolate external effects, other browser sessions, custom mid-request commits, or client retries after the window closes. Do not treat it as a general write sandbox.
- Local source search can match Han-language labels and comments, but does not translate a Chinese business question into English code identifiers. Cross-language recall has not been broadly qualified.
- Source-informed no-Skill form filling has a scripted contract test, but no independent model-backed success-rate result yet. It does not establish arbitrary page-operation coverage.
- When source lookup is enabled with a hosted model, retrieved allowlisted source is sent in model requests. Use that combination only where the deployment's data policy permits it.
- Browser and accessibility coverage is strongest on Chromium; other engines and screen readers are not comprehensively qualified.
- Yudao's experimental slim executor accepts a native date-range filter through `fill_fields` as `YYYY-MM-DD ~ YYYY-MM-DD`, including when only the start input has the field label. Both inputs must be visible and own the same calendar in the same live form. An individual `input` takes one endpoint's date. ERP sales-order time and the configuration and mall-order creation-time ranges are reported from successful page queries. Native selection preserves the application's default endpoint times, but does not navigate calendar months: a day absent from the displayed calendar, or disabled by the application, is refused.
- A reached page state is checked against what the Agent declared (path, selected controls, active filters or groups). This does not prove that all asynchronous loading has finished, and a requirement the declaration omitted can remain unmet.
- A model-selected query is not proven to cover the user's intent; an empty read is returned to the Agent rather than ending the turn as "not found".
- Free-text list search is answered from a scoped read rather than typed into the page's search box. A creation handoff only highlights the entry control; it does not prefill or submit.
- Evaluation results so far come from small samples of one reference deployment, several authored by the implementers; they do not establish general accuracy. See the [Odoo evaluation details](evals/odoo/README.md) and the [Yudao comparison results](evals/yudao/tool_loop/RESULTS.zh-CN.md).

## License

See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
