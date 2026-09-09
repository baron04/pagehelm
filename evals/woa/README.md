# Web Operation Agent evaluation runner

This directory runs the product itself as one arm of the Twenty comparison. It is outside `src/`, is not packaged with the product, and must run as a separate process. Product and Agent code must never import it.

`twenty_runner.py` executes one read-only query or open case through the real product path: `TwentyApplicationAdapter` identity binding, a capability-bound remote session channel, the packaged Business Skills and typed tools, and `AgentTurnExecutor`. It dequeues the panel message the way the conversation host does, so a typed boundary failure survives for classification instead of being flattened into display text. It never reads the database baseline, after-state, evaluator result, held-out task set, or another run, and it refuses create cases because this arm has no qualified write path.

`assembler.py` turns what that turn produced into the engine-neutral `woa_twenty_run` artifact consumed by `evals.twenty.trial`. It classifies the outcome with `evals.twenty.outcomes.classify_woa_outcome`, never by exception text: invalid tool arguments, budgets, a refused route, a refusal and cancellation stay scorable product outcomes, while identity, provider, business-API and transport faults become `apparatus_invalid` with the ledger's apparatus code. A replaced browser session invalidates even a claimed success, and an indeterminate write stays indeterminate. Query evidence comes only from the public user answer and open evidence only from the final visible page, both through the shared `evals.twenty.evidence` contract.

`ServerAgentSession` truncates its own `agent_said` at 500 characters, well below the turn policy limit and far below the 20,000 characters the PageAgent runner accepts. The artifact therefore reports `turn.answer_at_engine_ceiling` so a reviewer can see when an answer may have been cut by the engine rather than written short. This asymmetry must be resolved before a breadth benchmark whose workspaces are larger than the current fixture.

`scripted.py` is the model-free apparatus: a scripted chat with content-free accounting and injectable provider failures, a recording wrapper that keeps the run's own successful business results, and a simulated runtime that plays the browser over the real hello/lease, observation, claim and result protocol for a synthetic SPA whose list page exposes no company data. `browser_host.py` is the real alternative: the trusted Playwright host serves the embedded runtime from a loopback gateway bound to the runner's own registry, injects the bootstrap script into the authenticated page, and submits one goal through the panel API. It reads the document's own `location.href` rather than Playwright's cached URL, because a first-party SPA route change uses `history.pushState` and the cached value can still hold the previous route.

## Offline and browser-free tests

```bash
PYTHONPATH=. python -m unittest evals.woa.test_assembler evals.woa.test_twenty_runner evals.woa.test_qualify_schedule -v
```

`test_twenty_runner.py` drives the real turn path for success, a wrong or repaired answer, an open case without a disclosed route, invalid tool arguments, a business API outage, a provider outage with unknown cost, identity failure before launch, a replaced session, cancellation before and during a turn, and the step budget, then scores successful evidence with the independent evaluator.

## Common-driver qualification

`qualify_schedule.py` builds a frozen protocol over the public development task set and runs both engines through one `evals.twenty.driver`, evaluator, projector and append-only ledger, then recomputes the public report. A held-out-shaped task set must contain create tasks and the protocol must schedule every task, so the qualification pins a seed whose first six scheduled pairs are query and open, and drives that read-only prefix. Choosing a seed is legitimate for an apparatus artifact and is recorded in the module; the real benchmark seed must be preregistered independently of any outcome.

## Live no-model qualification

`qualify_live.py` runs one fixed public development query case and one open case through real Chromium, the real embedded runtime, the delegated Twenty API, the database before/after projection and the independent evaluator, with a scripted provider so it spends no model tokens. It asserts an unchanged read-only fixture before each case and writes only versions, digests, counts and Boolean checks.

```bash
PYTHONPATH=. python -m evals.woa.qualify_live \
  --base-url http://localhost:3020 \
  --storage-state .private/twenty-storage-state.json \
  --oauth-token .private/twenty-oauth-token.json \
  --oauth-registration .private/twenty-oauth-client.json \
  --page-controller-module http://localhost:3020/woa/page-controller.js \
  --audit-ledger .artifacts/evals/twenty/qualification/woa-live-audit.jsonl \
  --container twenty-db-1 \
  --database default \
  --db-user postgres \
  --workspace-schema workspace_example \
  --output .artifacts/evals/twenty/qualification/woa-live-runner.json
```

The base URL must be the origin the stored browser session cookie is scoped to; a host that does not match it fails identity binding before any model or browser work. The local Twenty 2.39.0 fixture passed both cases with zero paid calls. This qualifies the read-only apparatus; two scripted cases do not establish an Agent success rate or a comparative result.
