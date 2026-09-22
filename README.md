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
  prerequisite for ordinary browser or API operation.
- An application-neutral entity-read contract can discover a current actor's readable
  business entities and perform bounded reads without adding one Skill per entity. The Agent
  receives only catalog-bound opaque entity and field IDs; an adapter translates its ORM,
  REST, OpenAPI, or other native metadata behind the contract. The core validates an
  AND-only filter AST and pagination bounds; the contract requires stable adapter-owned
  ordering and exact counts. Odoo and the structurally different synthetic Contacts REST
  adapter run the same contract tests.
- An optional application-neutral current-form contract supports a no-Skill fallback for
  safe scalar form filling. `describe_form` intersects the latest viewport controls with
  actor-scoped adapter metadata and returns only short-lived opaque IDs; `prepare_form`
  rechecks both sides and retains the exact native bindings on the server; `prefill_form`
  types that draft and ends the turn without clicking or submitting. Odoo and Contacts
  translate different metadata sources to the same contract. An optional source lookup can
  inform a value for an existing field; a source-derived value carries a turn-local reference
  and exact supporting span for internal audit. This verifies provenance, not the model's
  interpretation. Source cannot create a field, selector, choice, permission, or write
  authority.
  The Odoo reference translation is experimental and remains an explicit adapter opt-in.
  Its three-case standard-module browser gate passes with zero submitted or created records,
  but that does not establish that arbitrary deployment-specific `onchange` code is inert.
- Turn context is assembled by source role: the current page is the live grounding baseline;
  bounded conversation history is included for referential or clarification follow-ups;
  Skills are selected from the current task; and deployment source remains a lazy cited
  lookup instead of being injected wholesale.
- Browser actions are checked against the current observation and application route policy.
  Page observations are viewport-bound and expose at most 200 live controls; a bounded
  read-only scroll obtains the next fresh observation instead of scanning an unbounded DOM.
  Page switches require a visible confirmation.
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
arguments. This does not authorize writes.

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

## License

See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
