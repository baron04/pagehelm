# Official PageAgent baseline adapter

This directory pins Alibaba PageAgentCore and PageController v1.12.4 as the external browser-agent baseline. It imports the official packages, disables PageAgent retries, `ask_user`, script execution, `/llms.txt`, the visual mask and parallel tool calls, and builds a browser bundle that delegates provider and business-tool calls to harness-owned Playwright bindings. Provider credentials never enter the page bundle.

The bridge supports two capability profiles. `browser_only` exposes PageAgent's official DOM tools without Skills or custom tools. `skill_api` compiles a bounded closed-object subset of the same JSON tool schemas used by Web Operation Agent and delegates execution to the harness; both compared systems must bind the same Skill and tool-catalog digests in the frozen protocol. Its generic `open_allowed_route` adapter can open only an exact `ui_path` disclosed by a successful business-tool result in the same run, so PageAgent and Web Operation Agent can be compared on API-to-UI tasks without giving either engine arbitrary route access. The bridge does not implement business behavior or modify PageAgent planning.

Install exactly the lockfile and build the ignored artifact:

```bash
npm ci
npm test
npm run build
```

`npm test` builds twice in separate temporary directories and requires identical manifests and bundle digests. `npm run build` writes `pageagent-baseline.js` and a dependency/integrity manifest under `.artifacts/evals/pageagent/`; generated bundles and `node_modules` are excluded from Git.

`host.py` binds the opaque browser run token to one server-held completion client and derives the PageAgent request options from frozen protocol values. It accepts only the exact synthetic provider URL, configured model and sampling/output options; it applies the shared guarded transport deadline and call budget, returns only the provider response, redacts raised errors, and exposes content-free accounting. `business_host.py` derives PageAgent instructions and tool schemas from the same loaded `BusinessSkill` and `BusinessToolRuntime` used by Web Operation Agent, verifies the frozen catalog digest, binds calls to an `OperationContext`, retains result-authorized routes and enforces cancellation and separate business/route budgets. Mutations require an explicit runner opt-in, return a digest-bound proposal first, display its exact effect and targets in a native browser confirmation, consume one decision, and dispatch at most once. `twenty_runner.py` composes these boundaries for query, open and explicitly enabled create cases, verifies the exact bundle and browser/API identity, and emits a private runner artifact with the engine-neutral fields consumed by `evals.twenty.trial`. `run_case` and `run_breadth_case` share one execution path and differ only in a task profile: what the model is told, whether the final page is sampled, and how a finished run becomes evaluator evidence. The breadth entry point covers the v2 enumerate, filter, open-record, open-collection and refusal kinds for companies, people and opportunities on the Skill/API profile; a refusal on this engine is the model finishing with `success: false` while dispatching no mutation, because PageAgent has no product-level scope gate. Exact frozen-schedule orchestration remains outside this process. Do not expose bridge bindings to an untrusted page; the current Twenty benchmark uses a first-party synthetic fixture.

After building, `qualify_bridge.py` uses scripted OpenAI-compatible responses in real Chromium, so it spends no model tokens. It proves that the official click tool changes the synthetic DOM, a custom Skill/API tool passes through the principal-bound product `BusinessToolRuntime`, an exact result-authorized route performs SPA navigation, request options reach the protected provider Host, every path re-observes, and PageAgent performs no retry. The output contains only versions, digests and Boolean checks.

```bash
PYTHONPATH=../.. python qualify_bridge.py \
  --bundle ../../.artifacts/evals/pageagent/pageagent-baseline.js \
  --output ../../.artifacts/evals/pageagent/bridge-qualification.json
```

For query cases, the model's final `done.text` must itself be the complete schema-fixed answer; the runner never repairs prose, Markdown, omissions or duplicate rows. For Skill/API open cases, the harness accepts page evidence only when one current business result disclosed the exact final route, the route Host permitted that same route, and the rendered page visibly contains that record's name and domain. Browser-only cases receive no Skill or business Host and leave correctness to the isolated evaluator. A completed create requires one visible approval, one dispatch and one same-actor readback; denial is a scored failure and an uncertain dispatch is indeterminate. Host reports and bridge metrics must agree exactly.

Every Host refusal observed at the page bindings is classified. Model-call budget, provider deadline, business-call and route budgets, a refused route and cancellation are the engine's own failures and produce `status: failed`. Any other provider fault, a failed business tool, a malformed bridge call, an identity-binding failure before launch and a replaced browser session produce `status: apparatus_invalid` with the batch ledger's apparatus code (`provider_outage`, `transport_contract`, `credential_or_identity`, `ledger_failure`), so an outage is never scored against PageAgent and an engine failure never seals a batch. `run_case` also accepts a `cancelled` event: once set, every further provider, business and route call is refused and the artifact reports a cancelled failure, never a completed run.

The application boundary emits stable content-free error codes for request rejection, identity/authentication, upstream availability and response-contract failures. PageAgent Host refusals and Web Operation Agent audit events feed the same classifier in `evals.twenty.outcomes`: invalid model arguments remain scorable engine failures, while identity, provider, API and transport faults invalidate the apparatus. Error detail stays out of the runner projection.

`scripted.py` provides the model-free test apparatus: a scripted OpenAI-compatible client with content-free accounting and injectable transport failures, a synthetic query/create Business Runtime built on the product `BusinessToolRuntime`, a synthetic adapter that digests the browser session cookie, a loopback SPA page, and `SyntheticBreadthPage`, a three-object SPA whose every route reads the shared record lists so PageAgent sees the same facts the simulated runtime renders. `test_twenty_runner.py` drives `run_case` through a fake Playwright and `test_twenty_runner_browser.py` executes the built bundle in real Chromium; together they cover browser-only query/open, Skill/API query/open, approved and denied creation, creation followed by permitted navigation, wrong answers, unauthorized routes, identity failure, budgets, cancellation and provider outage. The browser module skips in an ordinary local run when the bundle is missing or Chromium cannot launch. Qualification environments set `WOA_REQUIRE_PAGEAGENT_BROWSER=1`, which turns either missing prerequisite into a failure so CI cannot report a silently skipped browser qualification:

```bash
PYTHONPATH=. python -m unittest evals.pageagent.test_twenty_runner evals.pageagent.test_twenty_runner_browser -v
```

This synthetic qualification proves the runner contract independently of a provisioned application. A second no-model qualification runs fixed public development query/open cases through the built bundle, real Chromium, the delegated Twenty API, browser/API identity binding, the Docker-backed database projection and the independent evaluator. It asserts an unchanged read-only fixture before and after each case and writes only schema-fixed digests, counts and Boolean checks:

```bash
PYTHONPATH=. python -m evals.pageagent.qualify_twenty_runner \
  --bundle .artifacts/evals/pageagent/pageagent-baseline.js \
  --base-url http://127.0.0.1:3020 \
  --storage-state .private/twenty-storage-state.json \
  --oauth-token .private/twenty-oauth-token.json \
  --oauth-registration .private/twenty-oauth-client.json \
  --container twenty-db-1 \
  --database default \
  --db-user postgres \
  --workspace-schema workspace_example \
  --output .artifacts/evals/twenty/pageagent-live-qualification.json
```

The local Twenty 2.39.0 fixture passed both cases with zero paid calls. This qualifies the read-only apparatus; two scripted cases do not establish an Agent success rate or a comparative result.

Run one already-instantiated private execution case after provisioning the synthetic Twenty fixture and model environment:

```bash
PYTHONPATH=. python -m evals.pageagent.twenty_runner \
  --case .artifacts/evals/twenty/case.json \
  --bundle .artifacts/evals/pageagent/pageagent-baseline.js \
  --bundle-digest 'sha256:…' \
  --base-url http://127.0.0.1:3020 \
  --storage-state .private/twenty-storage-state.json \
  --oauth-token .private/twenty-oauth-token.json \
  --oauth-registration .private/twenty-oauth-client.json \
  --spend-ledger .artifacts/evals/twenty/spend.jsonl \
  --model-env .private/model.env \
  --output .artifacts/evals/twenty/pageagent-run.json
```
