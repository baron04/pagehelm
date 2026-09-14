# Odoo product evaluation apparatus (P7.4)

This directory holds the evaluation apparatus for the Odoo reference integration. It is outside `src/`, is not packaged, and product code must never import it. It measures whether a fixed Web Operation Agent composition passes a scoped product screening gate on declared synthetic Odoo 18 workflows, with an exploratory comparison against official PageAgent on single-turn read and navigation tasks. It does not attribute differences to an agent engine and does not support a non-inferiority or "90% success rate" claim.

The apparatus has been qualified without any external model call. No paid batch has been run, so nothing here is a success rate.

## Arms and capabilities

| Arm | Capability | Used for |
| --- | --- | --- |
| `pageagent_browser_only` | Official PageAgentCore 1.12.4 in real Chromium, signed in as the same synthetic Odoo user; built-in DOM tools only; no Skill, business API or assistant (the addon's assertion and assistant endpoints are refused in its browser context). Single-turn, read-only. | Exploratory read/navigation comparison on a preregistered subset |
| `woa_product` | The single-node Odoo deployment: ordinary Odoo login, addon bootstrap, assistant panel, server ReAct turn, product Skill selection and `load_skill`, typed Odoo tools under the actor's delegated API key, approval card, durable journal and same-actor readback. | Every supported task and the screening gate |

A PageAgent arm with the product's Skills and typed tools is not built. [`equivalence.py`](equivalence.py) inspects the pinned PageAgentCore distribution and the shipped product and records why the two cannot start from the same Skill catalog without a prompt-reading harness or a modified PageAgentCore. Capabilities are fixed by arm and actor delegation; no runner reads a task's evaluator specification.

## Components

- [`population.py`](population.py) generates the synthetic Odoo population from a 128-bit seed. Tasks name record *roles*, so the development and held-out sets share one generator with independently sealed seeds. [`odoo_seed.py`](odoo_seed.py) builds that population inside an isolated, demo-free Odoo database, freezes scheduled actions, issues per-user API keys and verifies every predicted reference, amount, state, date and invoice number before committing.
- [`tasks.py`](tasks.py) validates, seals and instantiates task sets. Each split has 30 families, five per stratum, with a structural fingerprint; families and fingerprints must be disjoint across splits while causal-shape and class distributions match. Instantiation returns a `RunnerInput` (messages, entry path, actor) separate from the `EvaluatorInput`.
- [`tasksets/`](tasksets) holds the public development set, both seals and the split verification. The held-out set and both seeds stay in ignored private storage.
- [`stack.py`](stack.py) and [`compose.p74.yml`](compose.p74.yml) run the isolated `woa-p74-*` Compose project on top of `deploy/odoo-single-node`. The overlay adds only a loopback RPC port for the evaluator and a provider relay inside the Agent's network namespace; the Agent gets a verified-empty state directory and a new provider token before every attempt.
- [`gateway.py`](gateway.py) is the only provider path for both arms. It binds requests to the open attempt, appends `model_request_dispatched` to the attempt ledger before forwarding, records the returned model string and usage afterwards, enforces the shared hard budgets and request parameters, and records any request that belongs to no attempt. Both arms must reach the provider with temperature 0, the output cap as `max_tokens`, the product's own model options for the requested alias and no `top_p`; PageAgent's server-held completion client maps its bridge request onto that set, and the gateway refuses any request that still differs. [`provider_relay.py`](provider_relay.py) forwards bytes from the Agent's loopback to the gateway.
- [`ledger.py`](ledger.py) is the append-only, hash-chained attempt ledger. It enforces one open attempt, a fixed record order, legal combinations of the three result axes from [`outcomes.py`](outcomes.py), bounded replacements that immediately follow their original, and mandatory seals after safety events, reset failures, unclassified results, model-string changes, unowned requests and exhausted replacement caps. A WOA reset also carries content-free Agent-state assertions: the verified-empty start, sessions and unresolved mutations after logout, and every still-prepared journal reservation counted as closed by the audit without dispatch or as open. Residue, or an end state that could not be read, never accompanies a passing reset; a recovered attempt is asserted after an Agent restart has ended its orphaned session.
- [`runner_woa.py`](runner_woa.py) and [`runner_pageagent.py`](runner_pageagent.py) drive real Chromium. The WOA runner signs in, opens the panel from its launcher, types each message, lets [`oracle.py`](oracle.py) decide the confirmation card, and reads only the default-visible answer of the newest turn. [`trace.py`](trace.py) projects both engines' traces onto one reported schema.
- [`evaluator.py`](evaluator.py) runs as a separate process with only the checkout root on its path. It computes truth through [`rpc.py`](rpc.py) under the trial's own actor with its own domains, and judges visible answers through [`extractor.py`](extractor.py), whose specification-derived variants in [`variants.py`](variants.py) must be judged without a single error.
- [`fixture.py`](fixture.py) digests whole rows of the business, accounting, chatter and notification tables, currency rates, `ir_sequence` rows and PostgreSQL sequence values, and every guarded filestore object; it snapshots and restores the database and filestore and verifies the result against both the guard and [`projection.py`](projection.py). [`manifest.py`](manifest.py) records every pinned fixture input.
- [`driver.py`](driver.py) orchestrates one attempt end to end. [`protocol.py`](protocol.py) is the machine protocol: arms, budgets, provider record, replacement, stop and batch-chain policies, the runner input boundary, the evaluator and reset rules, schedule and freezes. Its digest covers the SHA-256 of every file a formal batch runs, so each batch header binds the exact apparatus implementation, and the second freeze records the candidate commit with those digests. [`stats.py`](stats.py) and [`report.py`](report.py) recompute the public report with separate primary and replicate observations, Wilson and Newcombe intervals, sensitivity analysis and unknown-cost semantics. [`canary.py`](canary.py) is the provider preflight contract.
- [`qualify.py`](qualify.py) is the zero-model apparatus qualification.

## Offline tests

```bash
PYTHONPATH=src:examples/odoo/src:examples/twenty/src:. python -m unittest discover -s evals/odoo/tests -t .
```

## Live qualification

The qualification needs Docker, the pinned PageAgent bundle (`npm ci && npm run build` in `evals/pageagent`) and Playwright Chromium. Private inputs, seeds, the held-out set, archives, ledgers and evidence live under `.private/p74-odoo-v1/`, which Git ignores. Build the isolated stack from `deploy/odoo-single-node` with an explicit `woa-p74-*` project name, seed it with `odoo_seed.py`, take the baseline with `fixture.prepare_snapshot`, record the stability proof, then run:

```bash
PYTHONPATH=src:examples/odoo/src:examples/twenty/src:. python -m evals.odoo.qualify run
```

The command runs scripted zero-model samples on every stratum, negative controls, provider, evaluator, browser and pre-start fault injections with live replacement, sealing probes, wrong-reset rejections and an adapter domain mutation. It writes a private, content-free artifact of checks, counts and digests. Passing it qualifies the apparatus only.
