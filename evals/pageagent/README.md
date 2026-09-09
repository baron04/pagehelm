# Official PageAgent baseline adapter

This directory pins Alibaba PageAgentCore and PageController v1.12.4 as the external browser-agent baseline. It imports the official packages, disables PageAgent retries, `ask_user`, script execution, `/llms.txt`, the visual mask and parallel tool calls, and builds a browser bundle that delegates provider and business-tool calls to harness-owned Playwright bindings. Provider credentials never enter the page bundle.

The bridge supports two capability profiles. `browser_only` exposes PageAgent's official DOM tools without Skills or custom tools. `skill_api` compiles a bounded closed-object subset of the same JSON tool schemas used by Web Operation Agent and delegates execution to the harness; both compared systems must bind the same Skill and tool-catalog digests in the frozen protocol. The bridge does not implement business behavior or modify PageAgent planning.

Install exactly the lockfile and build the ignored artifact:

```bash
npm ci
npm test
npm run build
```

`npm test` builds twice in separate temporary directories and requires identical manifests and bundle digests. `npm run build` writes `pageagent-baseline.js` and a dependency/integrity manifest under `.artifacts/evals/pageagent/`; generated bundles and `node_modules` are excluded from Git.

This is the pinned browser-side adapter and supply-chain qualification, not a completed benchmark runner. A Playwright host must still bind the provider proxy, typed tools, identity, reset/evaluator workflow, trial record, cancellation and exact protocol schedule. Do not expose either bridge binding to an untrusted page; the current Twenty benchmark uses a first-party synthetic fixture.

After building, `qualify_bridge.py` uses scripted OpenAI-compatible responses in real Chromium, so it spends no model tokens. It proves that the official click tool changes the synthetic DOM, a custom Skill/API tool preserves validated arguments and returns its result, request options reach the provider binding, both paths re-observe and finish in exactly two steps, and PageAgent performs no retry. The output contains only versions, digests and Boolean checks.

```bash
PYTHONPATH=../.. python qualify_bridge.py \
  --bundle ../../.artifacts/evals/pageagent/pageagent-baseline.js \
  --output ../../.artifacts/evals/pageagent/bridge-qualification.json
```
