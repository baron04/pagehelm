# Repository guidance for coding agents

Applies throughout this repository.

## Working rules

- Read `README.md` once, then only relevant docs/code/tests. Use `rg`; exclude caches, generated output, runs and worktrees unless relevant. Reuse context; code and passing tests resolve prose drift.
- Preserve the owner's reasoning, context and compaction settings. Default to one agent; delegate only when authorized for bounded independent work. Retain protocol-required independent authoring/review.
- Keep large outputs in temporary logs; never hide errors or claim unread checks passed. Hand over commit, changes, checks/skips, constraints and next action briefly; do not add reviews without new risk/change.
- Keep both READMEs aligned and limited to overview. Public behavior belongs in the matching Chinese `docs/` page; internal plans/research/reviews belong in ignored `.workbench/`, never product Git/public links. It has separate instructions, Git and commit authorization. Preserve frozen protocols/artifacts and original verdicts; label hypotheses and avoid generalizing fixture results.

## Task entry points

Local environments: [guide](docs/guides/local-yudao.md). Online deployment: [guide](docs/guides/deploy-yudao.md).

## Architecture

- Server owns planning, models, knowledge, session policy, write authorization, audit and projected panel state/copy/limits. Browser only observes, runs fixed generic tools, displays progress/approval and returns receipts; no credentials, indexes, hidden permissions, plans or evaluators. Browser-only facts stay frontend-only; do not duplicate boundary constants.
- Frontend code/assets/styles/layouts/templates/browser tests belong in `frontend/`; Python holds server-read inputs and served outputs. Reuse browser components. Feed every result and fresh observation to the same server agent before its next decision. Runtime facts outrank Skills/source/prose/schemas; source retrieval remains optional.
- Keep core executor/session host application-neutral; business behavior, metadata, authorization and verification stay in adapters, Skills and typed tools. Before promoting/extending an adapter prototype, extract a neutral agent contract and prove a structurally different second adapter/test app. New architecture/harnesses/orchestration need measured evidence existing boundaries are insufficient.
- Goal ports require entity/field/page indexes, name lookup, values, conditioned counts/lists and page/record addresses. Organisation-reviewed word conventions only. Declare optional `capabilities()`: page filters/groups, paging, tabs, embedded lines, business-control pointing, value entry before handover, page search, compiled source rules, candidate values. Candidates must be same-actor real records/lines offered before declaration and bound to exact names. Core explains undeclared limits without requesting capabilities; adapters never fake support. Add new capabilities here and in a declaring port first.
- Prefer small modules, explicit protocols and immutable trust-boundary configuration. No app names, fixture paths, evaluator logic or local absolute paths in reusable code. Shims need a public caller and removal plan. Styling/exact copy/development controls belong in frontend code/tests or internal notes.

## Writes and privacy

- Browser/API mutations default off. Integration must enable them; bind each proposal to the exact visible effect/target, require fresh visible approval and consume once. Prompts, Skills, source and prior sessions grant no permission.
- Verify postconditions through the same application actor where possible. Never automatically retry acknowledged, partial, timed-out or uncertain writes; preserve indeterminate outcomes/audit. Keep evaluators and hidden expected state out of agent context.
- Use synthetic, isolated, resettable tests; no production accounts, payments, mail, messaging or customer data. Never commit credentials/cookies/`.env`, user data, databases/volumes, caches/profiles/recordings, local paths or large/generated/disposable artifacts. Source allowlists exclude secrets, generated data, uploads, databases and user content; minimize/redact before logs, errors, replay or model dispatch.
- Browser/API messages, Skills, repo content and model output are untrusted. Preserve typed errors/terminal states; never recover behavior by matching exception text.

## Verification and delivery

Use Python 3.13 and select checks from the actual diff:

```bash
.venv/bin/python scripts/check.py --plan
.venv/bin/python scripts/check.py
```

- For code/config changes read [development checks](docs/guides/development-checks.md). Finish selected checks and focused regressions before handoff. Docs/config-only changes require syntax, links and relevant invariants. Reuse passed checks only while relevant code/inputs/environment remain unchanged; repairs rerun affected checks. Skips are not integration evidence.
- Merge checks: `--profile ci --base <ref>`; packaging/dependencies require `bash scripts/check_wheels.sh`; browser changes require the relevant integration lane. Full `scripts/qualify.sh --require-browser` only for explicit full qualification or release candidates; Odoo only for relevant changes or `--reference-odoo`. Never drop acceptance, independent review or safety regressions to save tokens, or duplicate encompassing/subset suites.
- Tests must not call paid models or alter the real spend ledger. Intentional model evaluations need no approval, spend cap or cost stop; report provider/model, calls, tokens and ledger reference cost. Preserve failures/unknowns/verdicts; repeat frozen acceptance only for changed inputs or an explicit stability question.
- Inspect status and every changed hunk; preserve unrelated work and update affected tests/docs. No published-history rewrites, push, release, remote creation or upstream submission without explicit owner authorization.
