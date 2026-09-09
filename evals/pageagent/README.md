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

`host.py` binds the opaque browser run token to one server-held completion client and derives the PageAgent request options from frozen protocol values. It accepts only the exact synthetic provider URL, configured model and sampling/output options; it applies the shared guarded transport deadline and call budget, returns only the provider response, redacts raised errors, and exposes content-free accounting. `business_host.py` derives PageAgent instructions and tool schemas from the same loaded `BusinessSkill` and `BusinessToolRuntime` used by Web Operation Agent, verifies the frozen catalog digest, binds calls to an `OperationContext`, permits read tools only, retains result-authorized routes and enforces cancellation and separate business/route budgets. `twenty_runner.py` composes these boundaries for one real read-only Twenty query or open case, verifies the exact bundle and browser/API identity, and emits a private runner artifact with the engine-neutral fields consumed by `evals.twenty.trial`. Exact frozen-schedule orchestration remains outside this process. Do not expose bridge bindings to an untrusted page; the current Twenty benchmark uses a first-party synthetic fixture.

After building, `qualify_bridge.py` uses scripted OpenAI-compatible responses in real Chromium, so it spends no model tokens. It proves that the official click tool changes the synthetic DOM, a custom Skill/API tool passes through the principal-bound product `BusinessToolRuntime`, an exact result-authorized route performs SPA navigation, request options reach the protected provider Host, every path re-observes, and PageAgent performs no retry. The output contains only versions, digests and Boolean checks.

```bash
PYTHONPATH=../.. python qualify_bridge.py \
  --bundle ../../.artifacts/evals/pageagent/pageagent-baseline.js \
  --output ../../.artifacts/evals/pageagent/bridge-qualification.json
```

The Twenty runner deliberately refuses create cases until PageAgent has an approval boundary equivalent to the product path. For query cases, the model's final `done.text` must itself be the complete schema-fixed answer; the runner never repairs prose, Markdown, omissions or duplicate rows, and it fails the run when that answer does not restate exactly the companies and completeness that the run's own `query_companies` results disclosed. For open cases, the harness accepts page evidence only when one current business result disclosed the exact final route, the route Host permitted that same route, and the rendered page visibly contains that record's name and domain; the evidence is sampled from the final browser page, never from database truth. Host reports and bridge metrics must agree exactly.

Every Host refusal observed at the page bindings is classified. Model-call budget, provider deadline, business-call and route budgets, a refused route and cancellation are the engine's own failures and produce `status: failed`. Any other provider fault, a failed business tool, a malformed bridge call, an identity-binding failure before launch and a replaced browser session produce `status: apparatus_invalid` with the batch ledger's apparatus code (`provider_outage`, `transport_contract`, `credential_or_identity`, `ledger_failure`), so an outage is never scored against PageAgent and an engine failure never seals a batch. `run_case` also accepts a `cancelled` event: once set, every further provider, business and route call is refused and the artifact reports a cancelled failure, never a completed run.

The application boundary emits stable content-free error codes for request rejection, identity/authentication, upstream availability and response-contract failures. PageAgent Host refusals and Web Operation Agent audit events feed the same classifier in `evals.twenty.outcomes`: invalid model arguments remain scorable engine failures, while identity, provider, API and transport faults invalidate the apparatus. Error detail stays out of the runner projection.

`scripted.py` provides the model-free test apparatus: a scripted OpenAI-compatible client with content-free accounting and injectable transport failures, a synthetic `query_companies` Business Runtime built on the product `BusinessToolRuntime`, a synthetic adapter that digests the browser session cookie, and a loopback SPA page whose list view exposes no company data. `test_twenty_runner.py` drives `run_case` through a fake Playwright and `test_twenty_runner_browser.py` executes the built bundle in real Chromium; both cover success, wrong answer, unauthorized route, identity failure, budget exhaustion, cancellation and provider outage, and score successful evidence with the independent evaluator against public development tasks instantiated over the synthetic set. The browser module skips in an ordinary local run when the bundle is missing or Chromium cannot launch. Qualification environments set `WOA_REQUIRE_PAGEAGENT_BROWSER=1`, which turns either missing prerequisite into a failure so CI cannot report a silently skipped browser qualification:

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
