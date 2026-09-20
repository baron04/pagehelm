# Odoo product evaluation apparatus (P7.4)

This directory holds the evaluation apparatus for the Odoo reference integration. It is outside `src/`, is not packaged, and product code must never import it. It measures whether a fixed Web Operation Agent composition passes a scoped product screening gate on declared synthetic Odoo 18 workflows, with an exploratory comparison against official PageAgent on single-turn read and navigation tasks. It does not attribute differences to an agent engine and does not support a non-inferiority or "90% success rate" claim.

## Status

The machine protocol is `p74-odoo-v2`. The development batches run under `p74-odoo-v1` are archived in `.workbench` with their recomputed reports; a v2 reader does not read v1 ledgers. The held-out set is sealed and has not been run.

## Arms and capabilities

| Arm | Capability | Used for |
| --- | --- | --- |
| `pageagent_browser_only` | Official PageAgentCore 1.12.4 in real Chromium, signed in as the same synthetic Odoo user; built-in DOM tools only; no Skill, business API or assistant (the addon's assertion and assistant endpoints are refused in its browser context). Single-turn, read-only. Its final answer is the `done` action's text, read through the bridge. | Exploratory comparison on a preregistered subset of the held-out set |
| `woa_product` | The single-node Odoo deployment: ordinary Odoo login, addon bootstrap, assistant panel, server ReAct turn, product Skill selection and `load_skill`, typed Odoo tools under the actor's delegated API key, approval card, durable journal and same-actor readback. Its answer is what the panel shows by default. | Every supported task and the screening gate |

A PageAgent arm with the product's Skills and typed tools was ruled out before it was built: PageAgentCore fixes its tool set at construction, while the product selects its initial Skill catalog from the prompt, so the two cannot start from the same catalog without a prompt-reading harness. Capabilities are fixed by arm and actor delegation; no runner reads a task's evaluator specification.

The two arms' answers reach the evaluator through different channels: PageAgent's `done` text is never shown in the page, WOA's answer is what the panel shows. The comparison therefore pairs the arms only on tasks judged by the page they end on, and reports each arm's results on answer tasks apart.

## Running it

Everything runs from the repository root with `PYTHONPATH=src:examples/odoo/src:.`. Private inputs, seeds, the held-out set, the fixture archives, ledgers and evidence live under `.private/p74-odoo-v1/`, which Git ignores. The live steps need Docker, the pinned PageAgent bundle (`npm ci && npm run build` in `evals/pageagent`) and Playwright Chromium.

Offline tests, with the stack, the fixture, the browser runner and the evaluator process faked and everything else real:

```bash
python scripts/offline_suite.py --start-dir evals/odoo/tests --top-level . --shards 4
```

Provision the isolated `woa-p74-*` stack and its fixture. `--reuse-fixture` rebuilds only the candidate and keeps a fixture that still matches its manifest; `--destroy-first` rebuilds the fixture; `--population heldout` seeds the held-out split instead of the development one.

```bash
python -m evals.odoo.provision --reuse-fixture
```

Qualify the running candidate without any model call. The full qualification is needed whenever anything under `evals/` changed since the last commit that passed it; a candidate that changed only the product needs the smoke qualification (the scripted samples on both arms, the panel's submit and cancel feedback and the leak check). A held-out batch also needs the held-out fixture qualification, which reads no held-out task.

```bash
python -m evals.odoo.qualify run
python -m evals.odoo.qualify smoke
python -m evals.odoo.qualify heldout
```

Run a paid batch, watch it, stop it, and read its report:

```bash
python -m evals.odoo.batch run --confirm-paid-calls [--population heldout]
python -m evals.odoo.batch status
python -m evals.odoo.batch stop
python -m evals.odoo.batch report development-001
python -m evals.odoo.batch reconcile development-001 --owner NAME --billed-calls N --billed-cost-cny X --evidence-digest sha256:...
python -m evals.odoo.batch rearm --confirm-rearm
```

`run` refuses unless the checkout is clean and the running Agent image was built from it, the fixture manifest describes the running fixture and its recovery record is READY, the candidate has the qualification it needs on that manifest, the provider is configured in `.private/p74-odoo-v1/provider/provider.json` (`base_url` plus an owner-only `key_file`), the spend ledger can be written, and no earlier call's cost is still unknown. An open batch is resumed from its ledger under the bindings it started with, never restarted. `stop` seals the open batch with `operator_stop` before its next attempt. `reconcile` records the owner's reconciliation of a batch's unaccounted calls against the provider bill: the billed calls and cost and a digest of the bill export, never its content. `rearm` restores the fixture baseline after a failed or uncertain reset and verifies it; a baseline that cannot be restored voids the fixture, which is then provisioned again.

## What a batch measures

A batch runs all thirty tasks of a split on WOA, each observed twice (primary, then replicate), in an order derived from the candidate commit and never chosen. A held-out batch adds PageAgent on its eligible subset. Its ledger header, written before the first attempt, binds the protocol digest, the task-set seal, the fixture manifest and baseline guard, each arm's capability digest, the candidate commit and Agent image, the provider configuration and runtime binding, and the schedule with its seed. The protocol digest covers the SHA-256 of every file a batch runs.

- **Screening gate.** On `woa_product` primary observations of `normal` tasks, credits must reach ceil(0.9 n). The replicate is reported separately, never pooled, and cannot rescue a failed primary. A sensitivity computation scores every replaced slot as zero; a verdict that changes under it is not robust.
- **Refusal and limitation.** Reported per class with its own rate.
- **Safety.** Every event in every attempt counts, sealed batches included; the gate needs zero deterministic violations, and a one-sided 95% bound is reported.
- **Experience.** The nearest-rank P95 of automatic wall time must be at most 30 s on at least 20 normal read-only primary observations, and the candidate's zero-model qualification must show submit and cancel feedback within 300 ms.
- **Cost.** Every call is accounted to the spend ledger when it settles. Unknown usage makes the total cost null, never zero.

Only a complete batch whose header matches the plan it declared and whose schedule is the fixed thirty-task one is gated; any other batch is descriptive and says why. Paired comparisons are exploratory differences between product combinations with Newcombe intervals and exact McNemar p-values.

A held-out batch keeps what protects its conclusion. The task set was sealed and committed before anyone saw a result. Its schedule seed is derived from that seal and the candidate commit. A candidate runs the held-out set once; only a stop the product did not cause — a changed model string or an exhausted replacement cap — is continued by a new batch. While the batch is open, `status` shows only its progress and `report` is refused.

## How it works

- [`population.py`](population.py) generates the synthetic Odoo population from a 128-bit seed; [`odoo_seed.py`](odoo_seed.py) builds it inside an isolated, demo-free database and verifies every predicted reference, amount, state and date before committing.
- [`tasks.py`](tasks.py) validates, seals and instantiates task sets: 30 families per split, five per stratum, disjoint across splits. Instantiation returns a `RunnerInput` separate from the `EvaluatorInput`. [`tasksets/`](tasksets) holds the development set, both seals and the split verification.
- [`stack.py`](stack.py) and [`compose.p74.yml`](compose.p74.yml) run the isolated stack on top of `examples/odoo/deploy/single-node`. Images are built only from a clean checkout and labelled with its commit, which [`revision.py`](revision.py) checks.
- [`gateway.py`](gateway.py) is the only provider path for both arms. It binds each request to the open attempt, writes it to the ledger before forwarding, enforces the shared request contract and budgets, and types every provider result; attempts are classified from these records alone. Each dispatch is a lease that settles exactly once, and a call whose cost nobody knows holds the batch until its own late answer prices it or the owner's reconciliation covers it. [`provider_relay.py`](provider_relay.py) forwards the Agent's loopback calls to the gateway.
- [`ledger.py`](ledger.py) is the append-only, hash-chained attempt ledger. It enforces one open attempt, a fixed record order, legal combinations of the three outcome axes from [`outcomes.py`](outcomes.py), bounded replacements that immediately follow their original, and mandatory seals after safety events, reset failures, unclassified results, model-string changes, unowned requests, a refused credential, an unaccounted call and a request outside the contract.
- [`runner_woa.py`](runner_woa.py) and [`runner_pageagent.py`](runner_pageagent.py) drive real Chromium; [`oracle.py`](oracle.py) decides confirmation cards; [`trace.py`](trace.py) projects both engines' traces onto one reported schema.
- [`evaluator.py`](evaluator.py) runs as a separate process, computes truth through [`rpc.py`](rpc.py) under the trial's own actor and judges answers through [`extractor.py`](extractor.py).
- [`capture.py`](capture.py) types every fallible observation, so a source nobody could read is never taken for one read as empty; [`consumers.py`](consumers.py) lists every reader of each source, and the tests hold the code to it.
- [`fixture.py`](fixture.py) digests, snapshots, restores and verifies the fixture; [`manifest.py`](manifest.py) binds everything that decides what the fixture becomes.
- [`driver.py`](driver.py) runs one attempt end to end; [`harness.py`](harness.py) holds the bindings and setup every batch shares; [`batch.py`](batch.py) is the paid entry; [`protocol.py`](protocol.py) is the machine protocol; [`stats.py`](stats.py) and [`report.py`](report.py) recompute the report; [`qualify.py`](qualify.py) is the zero-model qualification.

## Limitations

Every report states these, whatever its numbers:

- The provider alias is not a model snapshot, and results hold only for the recorded alias and time window. A changed model string seals a batch, but no check can prove unchanged weights.
- The fixture is one synthetic application.
- The screening gate screens; it is not a reliability proof.
- The task set is balanced by stratum only, not by locale, actor or turn depth, and two observations per task support no per-task or per-action rate.
- The provider offers no usage export, so the absence of calls outside the apparatus on the same credential is unproven.

The held-out protections above are enforced by the batch entry and by the git history of the seal and the candidate. No external review record or freeze chain binds them.
