# Web Operation Agent evaluation runner

This directory runs the product itself as one arm of the Twenty comparison. It is outside `src/`, is not packaged with the product, and must run as a separate process. Product and Agent code must never import it.

`twenty_runner.py` executes browser-only and Skill/API query/open cases through the real product path: `TwentyApplicationAdapter` identity binding, a capability-bound remote session channel and `AgentTurnExecutor`. Skill/API cases add the packaged Business Skills and typed tools; browser-only cases receive neither. Explicitly enabled create cases use the same exact-effect approval and one-shot mutation path as the product. The evaluation runner declares the product's `test` deployment profile because its resettable apparatus uses a volatile journal; this qualifies mutation behavior but does not exercise production restart durability. Identity and approval remain mandatory. The runner dequeues the panel message the way the conversation host does, so typed boundary failures survive classification instead of being flattened into display text. It never reads the database baseline, after-state, evaluator result, held-out task set, or another run.

`assembler.py` turns what that turn produced into the engine-neutral `woa_twenty_run` artifact consumed by `evals.twenty.trial`. It classifies the outcome with `evals.twenty.outcomes.classify_woa_outcome`, never by exception text: invalid tool arguments, budgets, a refused route, a refusal and cancellation stay scorable product outcomes, while identity, provider, business-API and transport faults become `apparatus_invalid` with the ledger's apparatus code. A replaced browser session invalidates even a claimed success, and an indeterminate write stays indeterminate. Skill/API query evidence comes from the same public structured answer projection the product renders to the user, so incorrect model prose cannot add records or upgrade completeness; browser-only query evidence still comes from its public answer. Open evidence comes only from the final visible page. Both paths use the shared `evals.twenty.evidence` contract.

`breadth_assembler.py` and `run_breadth_case` add a separate v2 path for Companies, People, and Opportunities without changing the frozen v1 artifact. It supports enumeration, filtering, record opening, collection navigation, confirmed company creation, and explicit refusal of unsupported mutations. Collection navigation is authorized only after the corresponding typed query returns its exact `collection_ui_path`; merely appearing in a Skill or adapter route pattern does not permit direct navigation. `breadth_scripted.py` qualifies this behavior through the same product turn and remote-runtime protocol without contacting a model.

The product propagates the runner's 20,000-character answer limit into `ServerAgentSession`, matching the shared evidence contract and the PageAgent runner. The artifact reports `turn.answer_at_engine_ceiling` so a reviewer can still identify an answer that reached the common limit.

`scripted.py` is the model-free apparatus: a scripted chat with content-free accounting and injectable provider failures, a recording wrapper that keeps the run's own successful business results, and a simulated runtime that plays the browser over the real hello/lease, observation, claim and result protocol for a synthetic SPA whose list page exposes no company data. `browser_host.py` is the real alternative: the trusted Playwright host serves the embedded runtime from a loopback gateway bound to the runner's own registry, injects the bootstrap script into the authenticated page, and submits one goal through the panel API. It reads the document's own `location.href` rather than Playwright's cached URL, because a first-party SPA route change uses `history.pushState` and the cached value can still hold the previous route.

## Offline and browser-free tests

```bash
PYTHONPATH=. python -m unittest evals.woa.test_assembler evals.woa.test_twenty_runner evals.woa.test_breadth_runner evals.woa.test_qualify_schedule -v
```

`test_twenty_runner.py` drives the real turn path for success, model prose that disagrees with an authoritative typed-query projection, an open case without a disclosed route, invalid tool arguments, a business API outage, a provider outage with unknown cost, identity failure before launch, a replaced session, cancellation before and during a turn, and the step budget, then scores successful evidence with the independent evaluator.

## Common-driver qualification

`qualify_schedule.py` builds a frozen apparatus protocol over the public development task shapes and runs both engines through one `evals.twenty.driver`, evaluator, projector and append-only ledger, then recomputes the public report. It completes all 21 pairs and 42 trials across browser-only, Skill/API, the paired source interface and create tasks. Every create gets one synthetic user approval and an independently checked restore before the paired arm starts. The fixed apparatus seed is recorded in the module; a real benchmark seed must be preregistered independently of any outcome.

The paired Source OFF/ON tool surface is qualified, but the source treatment is not. The current SourceMap compiler and candidate resolver describe OFBiz controller routes, while the Twenty Companies tasks are already solvable from visible DOM or typed API results and do not establish a source-required fact. Source ON therefore delivers no useful Twenty candidate in this qualification. A compatible source artifact and a task population that truly depends on static hidden route, schema or rule knowledge are required before freezing H3; an empty-candidate transport run cannot establish source gain or no gain.

## Live no-model qualification

`qualify_live.py` runs one fixed public development query case and one open case through real Chromium, the real embedded runtime, the delegated Twenty API, the database before/after projection and the independent evaluator. `qualify_breadth_live.py` adds seven People and Opportunities enumeration, record-opening, collection-navigation, and refusal cases. Both use a scripted provider, spend no model tokens, assert unchanged read-only fixture state, and write only versions, digests, counts and Boolean checks. `scripts/qualify.sh --require-live` runs both.

`pilot_breadth.py` runs a fixed 12-case read/navigation/refusal development diagnostic with the configured paid model, a CNY stop line, per-case fixture and identity guards, and immediate stop on apparatus invalidity. `pilot_breadth_synthetic.py` exercises the same runner, schedule, and evaluator with randomly generated synthetic CRM records and a simulated browser protocol, so model behavior can be diagnosed without disclosing a local Twenty fixture. The summary artifact stores task IDs, typed outcomes, failed check names, usage, cost, latency, steps, action counts, refusal counts, safety counts, and digests; it stores no prompt, answer, page text, or fixture row. A completed pilot is diagnostic evidence and is never rendered as a benchmark success rate. After one earlier run exposed and helped repair an unnecessary post-query navigation, the recorded verification run completed 12 cases with 12 `PASS`, 0 `FAIL`, 0 `INVALID`, and CNY 0.433488 known cost; the result does not qualify real-browser model behavior.

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
