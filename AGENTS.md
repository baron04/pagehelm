# Repository guidance for coding agents

These instructions apply to the entire repository.

## Start with the public contract

Read `README.md`, then inspect the modules and tests that define the behavior you intend to change. Treat code and passing tests as the source of truth when prose has drifted. Keep the English and Chinese READMEs aligned when public behavior, setup, or limitations change.

## Preserve the architecture boundary

- Keep planning, model access, knowledge retrieval, session policy, write authorization, and audit state on the server.
- Keep the embedded browser runtime small. It may observe the page, execute fixed generic tools, show progress or approval UI, and return action receipts. It must not contain model credentials, source indexes, hidden permissions, business plans, or evaluators.
- Keep the assistant panel a view. What the server reads -- the states it projects, the text and paging limits it enforces, and the copy it sends -- is the server's; the panel is handed it and declares none of its own. What only the browser reads is the frontend's, and the server neither stores nor forwards it. A constant that exists on both sides is a defect, not a convenience.
- Keep frontend-only code and assets under `frontend/`: styles, design tokens, the window's sizes, position, and modes, page templates, and tests of browser-only behaviour. The Python package holds only what the server itself reads, plus the build outputs it serves.
- Feed each tool result and fresh page observation back to the same server-side agent before the next decision.
- Prefer current runtime facts over Skills, source code, documentation, and API schemas.
- Keep application-specific behavior behind adapters, Skills, typed tools, and application-owned verification. The core turn executor and live session host must remain application-neutral.
- Do not add an Agent-facing capability whose tool schema, prompt contract, control flow, or core policy assumes one application. A single adapter may host a measured prototype, but before that capability is treated as product architecture or extended further, extract an application-neutral contract and prove it with a second structurally different adapter or test application. Framework-specific source extractors, metadata translation, authorization, and verification remain in the application distribution.
- The goal port an adapter implements has two levels. Required: an index of entities, fields and pages (which may carry the deployment's reviewed conventions: what its organisation means by some words, never inferred from the application); records found by name; field values; counts and lists with conditions; the address of a page or record. Optional, each declared by name through `capabilities()`: page filters and groups, tabs, embedded lines, business controls to point at, entering a value before handing over, page search, rules compiled from source, and candidate values (real records and lines near what the request names, read under the same actor, offered before the declaration and bound back to exact names). The core answers a deliverable that needs an undeclared optional capability with a plain statement of what the application does not offer; it never asks the port for it, and an adapter never fakes it. A new optional capability enters the core only with its name in this list and a port that declares it.
- Source retrieval is optional. Do not make access to an application repository a prerequisite for ordinary browser or API operation.
- Reuse established browser automation components when possible. Do not build a new general browser harness or orchestration platform without evidence that the existing boundaries cannot support the requirement.
- Add architectural complexity only when a measured product need justifies it.

## Make writes fail closed

- Browser and API mutations are disabled unless the application integration explicitly enables them.
- Bind every proposed write to an exact effect and target summary shown to the user.
- Require a fresh, user-visible approval before dispatch.
- Consume approval once. Never infer permission from a prompt, a Skill, source code, or a prior session.
- Verify the postcondition through the application under the same actor when possible.
- Do not automatically retry an acknowledged, partially completed, timed-out, or otherwise uncertain write. Return an indeterminate outcome and preserve its audit record.
- Keep evaluators and hidden expected state out of the agent's context.

## Protect users and credentials

- Use synthetic data and isolated, resettable environments in tests and examples.
- Never commit tokens, cookies, passwords, `.env` files, production data, local caches, browser profiles, recordings, or application volumes.
- Read source only through an allowlist that excludes secrets, generated data, uploads, databases, and user content.
- Minimize browser-to-server payloads and redact sensitive values before logging, error reporting, replay generation, or model dispatch.
- Treat browser messages, API responses, Skill files, repository content, and model output as untrusted input.
- Do not connect automated tests to production accounts, payments, email, messaging, or customer data.

## Keep changes focused

- Keep `docs/` for public documentation. Store internal Q&A, research, reviews, and implementation plans in the Git-ignored `.workbench/`; do not commit these materials or link to them from public documentation.
- Prefer small modules with explicit protocols and immutable configuration at trust boundaries.
- Preserve typed errors and terminal states across layers; do not recover behavior by matching exception text.
- Avoid application names, fixture paths, evaluator logic, and local absolute paths in reusable package code.
- Avoid compatibility shims unless an existing public call site still needs them and the removal plan is clear.
- Keep generated files, raw experiment output, and disposable investigation notes out of Git. Delete obsolete material; place only useful private engineering notes in an ignored workspace.
- Document verified behavior and limitations. Label hypotheses as hypotheses, and do not turn a small fixture result into a general reliability claim.

## Test the behavior that changed

Use Python 3.12. Run the narrowest relevant test first, then the full product suite before handing off a completed code change:

```bash
python src/web_operation_agent/tests/run_all.py -k "test_product_*.py"
python src/web_operation_agent/tests/run_all.py
```

Choose the narrow pattern that matches your change. Runtime JavaScript tests require Node.js; browser-backed tests may also require the separately installed browser and page controller. A skipped environment test is not evidence that the integration passed.

When packaging changes, also build the wheel and confirm that required runtime assets are present and imports work outside the checkout:

```bash
python -m pip wheel --no-deps --wheel-dir dist .
```

Tests must not call paid models or alter the real spend ledger. Any intentionally executed model evaluation must record provider, model, call count, tokens, and actual cost.

## Commit hygiene

Before committing:

- inspect `git status` and the complete diff;
- keep unrelated user or collaborator changes intact;
- exclude caches, build output, credentials, local paths, and large generated artifacts;
- update tests and both public READMEs when behavior changes;
- report the exact checks run, including skips or environment limitations.

Do not rewrite published history, push, publish a release, create a remote repository, or submit an upstream change unless the repository owner has explicitly requested that action.
