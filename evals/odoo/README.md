# Odoo product evaluation apparatus (P7.4)

This directory holds the evaluation apparatus for the Odoo reference integration. It is outside `src/`, is not packaged, and product code must never import it. It measures whether a fixed Web Operation Agent composition passes a scoped product screening gate on declared synthetic Odoo 18 workflows, with an exploratory comparison against official PageAgent on single-turn read and navigation tasks. It does not attribute differences to an agent engine and does not support a non-inferiority or "90% success rate" claim.

The apparatus has been qualified without any external model call. No paid batch has been run, so nothing here is a success rate.

## Arms and capabilities

| Arm | Capability | Used for |
| --- | --- | --- |
| `pageagent_browser_only` | Official PageAgentCore 1.12.4 in real Chromium, signed in as the same synthetic Odoo user; built-in DOM tools only; no Skill, business API or assistant (the addon's assertion and assistant endpoints are refused in its browser context). Single-turn, read-only. Its final answer is the `done` action's text, read through the bridge. | Exploratory read/navigation comparison on a preregistered subset |
| `woa_product` | The single-node Odoo deployment: ordinary Odoo login, addon bootstrap, assistant panel, server ReAct turn, product Skill selection and `load_skill`, typed Odoo tools under the actor's delegated API key, approval card, durable journal and same-actor readback. Its answer is what the panel shows by default. | Every supported task and the screening gate |

A PageAgent arm with the product's Skills and typed tools is not built. [`equivalence.py`](equivalence.py) inspects the pinned PageAgentCore distribution and the shipped product and records why the two cannot start from the same Skill catalog without a prompt-reading harness or a modified PageAgentCore. Capabilities are fixed by arm and actor delegation; no runner reads a task's evaluator specification.

The two arms' answers reach the evaluator through different channels. PageAgent does not render its `done` text in the page, so its answer is a value no user sees, while WOA's is the one the panel shows. This does not affect the WOA-only development batches, but no formal PageAgent comparison runs until the channels match or the protocol records the difference and stops claiming one visible-answer rule for both arms.

## Components

- [`population.py`](population.py) generates the synthetic Odoo population from a 128-bit seed. Tasks name record *roles*, so the development and held-out sets share one generator with independently sealed seeds. [`odoo_seed.py`](odoo_seed.py) builds that population inside an isolated, demo-free Odoo database, freezes scheduled actions, issues per-user API keys and verifies every predicted reference, amount, state, date and invoice number before committing.
- [`tasks.py`](tasks.py) validates, seals and instantiates task sets. Each split has 30 families, five per stratum, with a structural fingerprint; families and fingerprints must be disjoint across splits while causal-shape and class distributions match. Instantiation returns a `RunnerInput` (messages, entry path, actor) separate from the `EvaluatorInput`.
- [`tasksets/`](tasksets) holds the public development set, both seals and the split verification. The held-out set and both seeds stay in ignored private storage, with an encrypted escrow outside the repository.
- [`stack.py`](stack.py) and [`compose.p74.yml`](compose.p74.yml) run the isolated `woa-p74-*` Compose project on top of `deploy/odoo-single-node`. The overlay adds only a loopback RPC port for the evaluator and a provider relay inside the Agent's network namespace; the Agent gets a verified-empty state directory and a new provider token before every attempt. Images are built only from a clean checkout and labelled with its full commit. [`revision.py`](revision.py) checks the running Agent image's `org.opencontainers.image.revision` against that clean HEAD, and the fixture manifest and every batch header record the verified candidate.
- [`gateway.py`](gateway.py) is the only provider path for both arms. It binds requests to the open attempt, appends `model_request_dispatched` to the attempt ledger before forwarding, records the returned model string and usage afterwards, enforces the shared hard budgets, and records any request that belongs to no attempt. It fails closed on request parameters: a request may carry only the shared chat-completion fields and the model's own option fields, with temperature 0, the product's option values for the requested alias and an integer `max_tokens` from one to the frozen cap. PageAgent's server-held completion client removes `top_p`, `max_completion_tokens` and the pinned bundle's `enable_thinking` patch and adds the options before sending. Input is bounded by request bytes rather than tokens, and every forwarded request's actual parameters are recorded. Behind the gateway sits a scripted backend or `UpstreamBackend`, the dedicated evaluation provider: one POST per request and never a retry, a request id sent as `x-woa-request-id`, and every dispatched call accounted to the spend ledger the way the product's own client accounts it, including an explicit usage-unavailable row. A timeout is a scorable `provider_deadline`; a dropped connection is an apparatus fault. Each backend names its provider id, a normalized endpoint digest and a domain-separated credential identity digest; with the requested alias and the request contract, that runtime provider binding is recorded by the preflight and bound by every batch header and attempt, so a changed endpoint or key needs a new preflight. [`provider_relay.py`](provider_relay.py) forwards bytes from the Agent's loopback to the gateway.
- [`ledger.py`](ledger.py) is the append-only, hash-chained attempt ledger. Its header binds the protocol digest, the task-set seal, the fixture manifest and baseline guard, a capability digest for every scheduled arm, the verified candidate, the runtime provider binding and the schedule with the seed a formal schedule was derived from; an attempt must use exactly the fixture, capability and provider binding its batch binds. The link to the previous batch names its purpose, candidate, chain head and ending. In a development chain a new candidate may follow any finished batch, while the same candidate continues only after an exogenous stop; held-out and qualification chains start again under the same protocol version only after an exogenous stop. It enforces one open attempt, a fixed record order, legal combinations of the three result axes from [`outcomes.py`](outcomes.py), bounded replacements that immediately follow their original, and mandatory seals after safety events, reset failures, unclassified results, model-string changes, unowned requests and exhausted replacement caps. A WOA reset also carries content-free Agent-state assertions: the verified-empty start, sessions and unresolved mutations after logout, and every still-prepared journal reservation counted as closed by the audit without dispatch or as open. Residue, or an end state that could not be read, never accompanies a passing reset; a recovered attempt is asserted after an Agent restart has ended its orphaned session.
- [`runner_woa.py`](runner_woa.py) and [`runner_pageagent.py`](runner_pageagent.py) drive real Chromium. The WOA runner signs in, opens the panel from its launcher, types each message, lets [`oracle.py`](oracle.py) decide the confirmation card, and reads only the default-visible answer of the newest turn; the PageAgent runner reads the `done` text its bridge returns. [`trace.py`](trace.py) projects both engines' traces onto one reported schema.
- [`evaluator.py`](evaluator.py) runs as a separate process with only the checkout root on its path. It computes truth through [`rpc.py`](rpc.py) under the trial's own actor with its own domains, and judges each arm's final answer through [`extractor.py`](extractor.py), whose specification-derived variants in [`variants.py`](variants.py) must be judged without a single error.
- [`fixture.py`](fixture.py) digests whole rows of the business, accounting, chatter and notification tables, currency rates, `ir_sequence` rows and PostgreSQL sequence values, and every guarded filestore object; it snapshots and restores the database and filestore and verifies the result against both the guard and [`projection.py`](projection.py). [`manifest.py`](manifest.py) records every pinned fixture input, including the candidate it was taken with; a batch refuses a manifest taken with another candidate.
- [`driver.py`](driver.py) orchestrates one attempt end to end. [`protocol.py`](protocol.py) is the machine protocol: arms, budgets, provider record, replacement, stop and batch-chain policies, the runner input boundary, the evaluator and reset rules, schedules and the formal entry's prerequisites. Its digest covers the SHA-256 of every file a formal batch runs, so each batch header binds the exact apparatus implementation. [`freeze.py`](freeze.py) records the three freezes. [`stats.py`](stats.py) and [`report.py`](report.py) recompute the public report over a whole batch chain, with separate primary and replicate observations, Wilson and Newcombe intervals, sensitivity analysis and unknown-cost semantics. A batch gets a screening verdict only when no later batch supersedes it, it is complete, its purpose and every bound digest equal the frozen plan, and its schedule is the fixed thirty-task schedule; every other batch is descriptive and says why. A report refuses a development chain in which a candidate returns after another candidate ran. [`canary.py`](canary.py) is the provider preflight contract; the canary carries the same request parameters as every arm.
- [`harness.py`](harness.py) holds what every batch runs with: private inputs, verified header bindings, gateway budgets and driver setup. [`formal.py`](formal.py) is the paid preflight and the formal development batch entry, and [`escrow.py`](escrow.py) keeps an encrypted, verified copy of the held-out set and both seeds and records its custody across independent failure domains.
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

The command runs scripted zero-model samples on every stratum, negative controls, provider, evaluator, browser and pre-start fault injections with live replacement, sealing probes, wrong-reset rejections and an adapter domain mutation. It also checks the formal closure on the checkout it runs from: the request contract, the upstream provider path against a loopback responder, the runtime provider binding, the candidate every header binds, forged bindings, report gating, the development and held-out chain rules, the freeze state machine, the escrow and its custody, the formal entry's refusals, and that neither project spend ledger changed. Every run writes its own private, content-free artifact of checks, counts and digests and never overwrites an earlier one. Passing it qualifies the apparatus only.

## Freezes, escrow and the formal entry

The protocol freezes three times. [`freeze.py`](freeze.py) makes each freeze a sealed record that names the freeze before it, and `protocol.document()` refuses a record of the wrong kind, one changed after it was sealed or one that does not link to its predecessor:

1. The first freeze binds both task-set seals, their split verification and an independent family review; only an `ACCEPT` review opens it.
2. The second binds the candidate — its commit, the Agent image labelled with that commit, the apparatus and machine-protocol files read from the commit itself, and the capability, provider and statistics digests — and is taken from the candidate's own clean checkout.
3. The third derives the held-out schedule seed from the commit that recorded the second freeze, fixes that schedule and seals the protocol document.

Before any paid call, the held-out set and both seeds are escrowed outside the repository, with the archive and its secret in different directories:

```bash
python -m evals.odoo.escrow create --archive /outside/the/repository/p74.escrow \
  --secret /elsewhere/outside/the/repository/p74.secret --record .private/p74-odoo-v1/escrow.json
python -m evals.odoo.escrow verify --archive /outside/the/repository/p74.escrow \
  --secret /elsewhere/outside/the/repository/p74.secret --private-dir .private/p74-odoo-v1
```

The archive is encrypted with AES-256-CBC under a PBKDF2-SHA256 key derived from a random 256-bit secret and authenticated with HMAC-SHA256. Nothing is overwritten, and `restore` writes only into a new, empty directory.

Two directories on one machine are not custody. Once a copy of the archive and a copy of its secret sit elsewhere, `custody` restore-verifies them and writes a record, holding neither paths nor secrets, of three failure domains: the original's, the archive's and the secret's, which must all differ:

```bash
python -m evals.odoo.escrow custody --archive /Volumes/Backup/p74.escrow \
  --secret /Volumes/Keys/p74.secret --record escrow-custody.json
```

A domain the machine can prove is a physical device, read from the operating system's disk description with disk images refused, or a network volume served by another host. Where it cannot prove a domain, for example a secret kept in a password manager, `--secret-attested LABEL --attested-by NAME` records an explicit owner attestation, keeping only a digest of the label. The record is committed once under `phase-7/P7.4/custody/` in `.workbench`.

[`formal.py`](formal.py) is the only entry that makes paid calls:

```bash
python -m evals.odoo.formal status
python -m evals.odoo.formal preflight --confirm-paid-calls
python -m evals.odoo.formal development --confirm-paid-calls
python -m evals.odoo.formal report
```

`status` sends nothing and names what is missing. Every paid step first requires `--confirm-paid-calls`, a clean checkout, the first freeze and its family review committed exactly once under `phase-7/P7.4/freezes/` in `.workbench`, a committed custody record that still matches the private held-out set and seeds, and an evaluation provider in `.private/p74-odoo-v1/provider/provider.json` (`base_url` plus an owner-only `key_file`). The preflight sends the canary five times, accounts the calls to the product spend ledger under their own batch id and keeps only content-free facts, including the runtime provider binding. A development batch also requires the newest preflight to have passed under the same freeze and the same runtime binding, the running Agent image to be built from this clean checkout with a fixture manifest taken on it, and the development task set to match its seal. Its schedule is derived from the first-freeze commit; its header is committed to `.workbench` before the first attempt and its chain head when it ends; a model string the preflight did not see seals it; an interrupted batch resumes from its ledger. A candidate runs one development batch: after it completes or seals for a product reason, the next batch needs a new candidate commit, and a candidate never returns once another has followed it. `report` recomputes the public report over the whole development chain and gates only its newest batch, against the first freeze, the header that batch announced and the preflight it relied on. Held-out batches are not available yet: they need the second and third freezes and matching answer channels.
