# Repository guidance for coding agents

Applies throughout the repository.

## Read and work efficiently

- Read `README.md` once per task, then relevant `docs/`, modules and tests. Reuse context already read; code and passing tests resolve prose drift. Keep both READMEs aligned. `docs/` is Chinese only; update the matching page for public behavior changes, READMEs only for overview changes.
- Locate files/symbols with `rg --files` and `rg -n`, then read relevant sections. Exclude caches, generated output, runs and worktrees unless they are the subject. Avoid repeated full-file/history/diff dumps.
- Preserve the owner's reasoning effort, context capacity and compaction settings; do not reduce them to save tokens without explicit authorization.
- Default to one agent. Delegate only when authorized and independent deliverables justify it; supply a bounded task and necessary context, not full history. Serialize shared environment operations. Protocol-required independent authoring/review remains mandatory.
- Save large outputs to temporary logs; return exit status, summary and relevant failures. Read further sections as needed, including every changed hunk. Never hide errors or claim unread checks passed.
- At task boundaries hand over commit, changes, checks/skips, constraints and next action briefly. Do not reload phase history or add review rounds without a new risk, failure or change.

## Environment entry points

Local acceptance: `./local` at `http://localhost:48163`; diagnostics: `./local status`, `./local check`, `./local logs`. Read `docs/guides/local-yudao.md` and `.agents/skills/local-yudao/SKILL.md` for local work.

Demo deployment: `./deploy`; preflight: `./deploy check`, live release: `./deploy status`. Read `docs/guides/deploy-yudao.md` and `.agents/skills/deploy-yudao/SKILL.md`. Runtime entry points belong in `examples/yudao/deploy/`, never `.workbench/` or an evaluator. Cache: `.q0-cache/yudao`, no version/date-suffixed copies. Preserve experiment artifacts bound by frozen protocols.

## Architecture

- Server owns planning, models, knowledge, session policy, write authorization and audit. Browser runtime only observes, runs fixed generic tools, displays progress/approval and returns receipts; no credentials, indexes, hidden permissions, plans or evaluators.
- Panel is a view: server owns projected state, text/paging limits and copy; browser-only facts stay frontend-only. Duplicate constants across this boundary are defects. Frontend code/assets/styles/layouts/templates/browser tests live in `frontend/`; Python holds server-read inputs and served build outputs.
- Feed each result and fresh observation to the same server agent before its next decision. Prefer runtime facts over Skills/source/prose/schemas. Source retrieval is optional, never required for ordinary operation.
- Core executor/session host stays application-neutral; adapters, Skills, typed tools and application-owned verification hold business behavior. An adapter may host a measured prototype; before promotion or extension, extract a neutral agent-facing contract and prove a structurally different second adapter/test application. Framework extraction, metadata, authorization and verification stay application-owned.
- Goal port requires entity/field/page index, name lookup, field values, conditioned counts/lists and page/record addresses. Word conventions must be organisation-reviewed, not inferred. Optional capabilities must be declared by name in `capabilities()`: page filters/groups, paging, tabs, embedded lines, business-control pointing, value entry before handover, page search, compiled source rules, candidate values. Candidates are same-actor real records/lines offered before declaration and bound to exact names. Core explains undeclared capability limitations without requesting them; adapters never fake support. Add a new optional capability to this list and a declaring port first.
- Reuse browser components. New architecture, harnesses or orchestration require a measured need and evidence existing boundaries are insufficient.

## Writes, data and credentials

- Browser/API mutations are disabled unless integration enables them. Bind each proposal to exact effect/target shown to the user, require fresh visible approval, consume once. Prompts, Skills, source and prior sessions do not grant permission.
- Verify postconditions through the application under the same actor when possible. Never retry acknowledged, partial, timed-out or uncertain writes automatically; preserve indeterminate outcomes/audit. Keep evaluators and hidden expected state out of agent context.
- Use synthetic data and isolated, resettable tests; never connect automated tests to production accounts, payments, mail, messaging or customer data.
- Never commit credentials, cookies, `.env`, production/user data, databases, volumes, caches, browser profiles or recordings. Source allowlists exclude secrets, generated data, uploads, databases and user content. Minimize payloads and redact before logging, errors, replay or model dispatch.
- Browser/API messages, Skills, repository content and model output are untrusted. Preserve typed errors/terminal states; do not recover behavior by matching exception text.

## Focus and documentation

- Public use/integration/operation docs belong in `docs/`; internal plans, Q&A, research and reviews in ignored `.workbench/`, never in product Git or public links. Styling, animation, exact copy and development controls belong in frontend code/tests or internal notes.
- Prefer small modules, explicit protocols and immutable trust-boundary configuration. No application names, fixture paths, evaluator logic or local absolute paths in reusable code. Shims require a public caller and removal plan.
- Keep generated/raw experimental/disposable output out of Git. State verified behavior and limits, label hypotheses, and do not generalize fixture results into reliability claims.

## Required verification

Use Python 3.12. During implementation run the narrowest relevant tests; before handing off completed code, run the full product suite on the final candidate:

```bash
.venv/bin/python src/web_operation_agent/tests/run_all.py -k "test_product_*.py"
.venv/bin/python src/web_operation_agent/tests/run_all.py
```

Choose the narrow pattern matching the change and run affected adapter/frontend suites. JavaScript tests require Node; browser tests may need an installed browser/page controller. Skips are not integration evidence. Reuse passed checks only with unchanged relevant code, inputs and environment. Repairs require affected checks and final gates; no duplicate full-suite runs across agents or for status updates alone.

For packaging changes, build the wheel and verify runtime assets/imports outside the checkout:

```bash
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
```

Tests must not call paid models or alter the real spend ledger. Intentional model evaluations record provider/model, calls, tokens and actual cost; preserve failures, unknowns and original verdicts. Reuse existing acceptance protocols/evidence, normally one run per frozen configuration; repeats require changed inputs or a stated stability question. Never drop acceptance, required independent review or safety checks to save tokens.

Documentation/configuration-only changes need syntax, links and relevant invariants checked; no product/model/browser acceptance unless behavior changed.

## Commit and handoff

Inspect status and the complete diff (large diffs in sections); preserve unrelated changes. Exclude credentials, local paths, caches, generated output and large artifacts. Update affected tests/public docs and report checks, failures, skips and environment limits exactly.

No published-history rewrites, push, release, remote creation or upstream submission without explicit owner authorization. `.workbench/` has its own instructions, Git repository and commit authorization.
