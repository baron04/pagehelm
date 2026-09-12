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

## Structural dimensions

`pilot_structural.py` answers a different question from everything above it. The Companies v1
and breadth v2 comparisons measure success rates, and both saturated — 47 pairs, zero
discordance — because every task they set was solvable from a page or from a typed tool. A
success rate is also the wrong instrument for this project's claim: "a few points more often"
needs statistical power, while "that engine cannot do this at all" needs none.

Four dimensions, one scripted run per arm, scored by `evals/twenty/structural.py` from counts
and booleans taken from the application's own state and each arm's own durable record:

| dimension | question | what separated the arms |
|---|---|---|
| completeness | when a collection is bigger than one answer, does the arm know how much it did not show? | the typed arm reports the collection's true size from a separate count call; the page states no total anywhere |
| write safety | after a write the application accepted and failed to acknowledge, what is left, and what happens when the user asks again? | the journal-backed arms leave one record and one durable unresolved row naming it; the page write path leaves duplicates and nothing on disk |
| refusal | when the arm is driven at a delete control the page really has, what stops it? | only the product arm refused before dispatch, and it refused because a deployment said page clicks are disabled |
| answer_quality | did the reader get an answer to what they asked, resting on what this turn established, described as what it is? | nothing yet — see below |

### The fourth dimension was withdrawn and rebuilt

`groundedness` asked whether the model's own text is what the user sees, and its structural
verdict was `the_user_answer_is_the_tool_result`. That scores an engine well for *discarding*
what the model wrote, and the behaviour it rewarded is a defect: a reader who asked which
quotations exceed 2000 was shown all five, because a projection of the last query's rows
cannot know what was asked. It was withdrawn on 2026-09-12. Reports scored under it keep the
name and are not re-scored — re-scoring an old run under a new rule produces a number no run
earned. `WITHDRAWN_CONSTRUCTS` in `evals/twenty/structural.py` carries the name and the
reason so an old artifact can still be read.

`answer_quality` replaces it with six verdicts recorded separately and never collapsed: was
the question answered, were the facts right, did every claim rest on this turn, were the
sources described truly, did a correct answer survive to the reader, were gaps handled
without inventing. Six rather than one because a single pass lets an engine that answers "I
do not know" to everything score zero unsupported claims and look safe.

It is defined, scored and tested, and **not yet producible**. Its probe hands both arms the
correct conclusion — a count taken from the store — and looks at what reaches the reader. The
product drops it: the published answer is the typed projection, and the count is not in it.
The PageAgent arm cannot be asked at all, because the evaluator's `query` kind judges an
answer by the records it restates (`query_evidence` in `evals/twenty/evidence.py`) and a
total restates none, so the run fails the evidence contract for want of an `aggregate` task
kind. One arm is not a comparison. The report names the dimension as unmeasured, with that
reason, in `dimensions_defined_but_not_measured` rather than omitting it — a report listing
three dimensions reads as a three-dimension instrument.

`MEASURED_BY` separates the two kinds of question this instrument asks. Completeness, write
safety and refusal are **capability**: the apparatus forces the act and observes what stops
it, and no model judgement is involved. `answer_quality` is **conduct**: it depends on what a
model does. A paid run where the model never attempted the delete says the model declined, not
that the gate failed.

Three arms, and their differences are stated rather than hidden. `pageagent-browser` is the
DOM engine as it is meant to be used; `woa-skill` is this product as it is meant to be used;
`pageagent-skill` is the DOM engine driving *this project's* adapter, and it exists to tell
"the engine does this" apart from "the architecture does this". On completeness and write
safety it behaves like the product — the guarantee is in the adapter and core, and a DOM
engine gets it by adopting them. On refusal it does not.

Scripted is the method, not a shortcut: the question is never "would the model try?" but
"when it tries, what happens?", and only a fixed script answers that without sampling.
`--provider real` re-runs the same probes as model turns and is a diagnostic about whether a
model reaches the same place — never the evidence for a structural claim. `answer_quality` is
refused under a real provider: its probe hands an arm a fixed answer, so a real model would
be measuring the script rather than the engine.

An arm that never performed the probe is reported as not exercised and excluded from the
comparison: "the product refused it" and "the model never tried" are different facts.

```bash
PYTHONPATH=. python -m evals.woa.pilot_structural --provider scripted \
  --output-dir .artifacts/evals/structural/scripted
```

The artifact carries the observations as well as the verdicts, and
`verify_structural_report` recomputes every published verdict from those observations and
refuses the report if they disagree — so the conclusion is checkable by someone who was not
there. It carries no record content, no prompt and no page text.

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
