# Web Operation Agent

[简体中文](README.zh-CN.md)

Web Operation Agent is an alpha framework for embedding an AI assistant in a first-party web application. Planning, model access, credentials, policy, approvals, and audit state stay on the server. A small browser Runtime observes the current page, executes fixed generic actions, displays progress and confirmation UI, and returns receipts.

The repository carries two reference integrations under [`examples/`](examples/): [Odoo 18](examples/odoo/README.md), kept as a reference and no longer maintained, and [Yudao](examples/yudao/deploy/README.md), the default demo and test application with a resettable local deployment. It is not a hosted service, a general browser extension, or a production-ready autonomous agent.

## Current capabilities

- Goal-directed turns: the model declares the request against the application's index; the server resolves it deterministically (records, field values, counts and sums, pages, tabs, filters) and renders the reply from resolved values. Ambiguous records are asked about; a turn completes only when every declared part settles.
- Form prefill with hand-over: enters the person's values, checks what each field then shows, highlights submit, never submits. Opt-in per application.
- Optional compiled source rules with cited answers, plus organisation-reviewed word conventions.
- An application-neutral model-driven loop; Yudao uses only `slim`, with index warming enabled by default. The core also retains the goal-directed and step-wise executors for other integrations.
- Application-owned Skills (Markdown knowledge and workflows) plus registered typed tools; knowledge and Skills never grant authority.
- Writes are disabled by default. When enabled, each write binds a complete visible approval, dispatches at most once, and is verified through the application; uncertain writes are never retried automatically.
- The React assistant panel is a view over server state, supports multi-page sessions, and can persist conversation state and mutation journals in optional SQLite stores.

These are implemented-path claims, not a general success-rate claim. Detailed behavior, executor differences and per-integration specifics: [Runtime behavior in detail](docs/reference/runtime-behavior.md).

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

Documentation: [docs/](docs/README.md) — concepts (why), guides (how), and the current-behavior reference.

- [System overview](docs/concepts/overview.md) · [Executors](docs/concepts/executors.md) · [Write model](docs/concepts/writes.md) · [Skills and typed tools](docs/concepts/skills-and-tools.md)
- [Integrate an application](docs/guides/integrate-an-application.md) · [Operate durably](docs/guides/operate-durably.md)
- [Runtime behavior in detail](docs/reference/runtime-behavior.md)
- [Security boundary](SECURITY.md)

## Deploy the Yudao demo

For local acceptance, run **`./local`**. It starts or updates the assistant at
`http://localhost:48163`, reuses the local synthetic application and skips unchanged
frontend builds. `./local status`, `./local check`, and `./local logs` provide diagnostics.
See the [local guide](docs/guides/local-yudao.md); the repository Skill is `$local-yudao`.

Once the host and this checkout's private configuration are ready, deploy with **`./deploy`**.
The cache lives at `.q0-cache/yudao`; no arguments or environment variables are needed.
Use `./deploy check` for preflight and `./deploy status` for the live release.
See the [deployment guide](docs/guides/deploy-yudao.md) for initial setup and recovery.
The repository Skill `$deploy-yudao` uses the same entry point.

## Repository layout

```text
src/web_operation_agent/       application-neutral server and browser Runtime
examples/odoo/                 reference integration (Odoo 18), no longer maintained
  src/web_operation_agent_odoo/  installable adapter
  deploy/reference/              disposable local Odoo fixture
  deploy/single-node/            operated single-node example
  deploy/addons/                 shared Odoo Runtime addon
examples/yudao/                default demo and test application, with a resettable deployment
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
- Yudao's native date-range filter has specific input-visibility and calendar constraints, and a day outside the displayed or enabled calendar is refused (details in [runtime behavior](docs/reference/runtime-behavior.md)).
- A reached page state is checked against what the Agent declared (path, selected controls, active filters or groups). This does not prove that all asynchronous loading has finished, and a requirement the declaration omitted can remain unmet.
- A model-selected query is not proven to cover the user's intent; an empty read is returned to the Agent rather than ending the turn as "not found".
- In the goal-directed path, free-text list search uses a scoped read rather than the page's search box, and a creation-entry handoff only highlights the control. Reviewed form-prefill paths can enter values and hand over without submitting.
- Evaluation results so far come from small samples of one reference deployment, several authored by the implementers; they do not establish general accuracy. See the [Odoo evaluation details](evals/odoo/README.md) and the [Yudao comparison results](evals/yudao/tool_loop/RESULTS.zh-CN.md).

## License

See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
