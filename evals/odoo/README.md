# Odoo product evaluation apparatus (P7.4)

This directory holds the evaluation apparatus for the Odoo reference integration. It is outside `src/`, is not packaged, and product code must never import it. It measures whether a fixed Web Operation Agent composition passes a scoped product screening gate on declared synthetic Odoo 18 workflows, with an exploratory comparison against official PageAgent on single-turn read and navigation tasks. It does not attribute differences to an agent engine and does not support a non-inferiority or "90% success rate" claim.

## Status

The machine protocol is `p74-odoo-v2`. The development batches run under `p74-odoo-v1` are archived in `.workbench` with their recomputed reports; a v2 reader does not read v1 ledgers. The held-out set is sealed and has not been run.

The separate one-sentence Chinese goal holdout is
[`tasksets/odoo-goals-zh-v4-holdout.json`](tasksets/odoo-goals-zh-v4-holdout.json):
116 reachable goals in 58 entity/delivery/condition cells (two per cell) and 15
honest-limitation goals. Its digest is recorded in the adjacent seal and pinned by
`evals.odoo.goals.run_goals`. The relative-time truth is based on 2026-09-24 in
Asia/Shanghai; an evaluation run must hold the application's clock to that date.
The handoff oracle requires the final page, the named live control's outline, and an
unchanged database fingerprint. Select it with `--version v4` when running
`python -m evals.odoo.goals.run_goals`. Its first complete model run, bound to
candidate `0f45d29`, recorded 46/131 under the original judge (37/116 reachable,
9/15 honest limitations), 10 false-claim marks, and no writes. A post-run judge
correction recognizes three successful handoffs whose expected safety sentence
said the assistant could not click for the user, yielding 49/131. Separately,
52 failed attempts received a `stale_observation` action receipt during Odoo's
initial `/odoo` redirect; no successful attempt did. This suite has now been
used and is not a fresh holdout for a subsequent candidate. The raw run remains
an immediate-submit result, not a settled-page accuracy estimate.

The second holdout is `tasksets/odoo-goals-zh-v5-holdout.json`, selected with
`--version v5`: 84 normal goals (18 record lists, 16 detail-line answers, 16 field
answers, 16 handoffs, 10 page goals, and 8 aggregates) plus 10 honest limitations.
Its adjacent seal pins the task bytes, input-data digest, vocabulary revision,
coverage, and offline validation. Both holdouts remain unchanged after sealing.
The reference dump has 42 accounting lines, of which 20 have a product; v5 detail
truth uses those product lines, not all 42. No model calls or full browser oracle
walk were performed for v5. The inherited impossible-goal judge does not establish
that an explanation is adequate, so limitation answers need separate review.
Detail oracles expect each product and its values on one text or table row; list
exclusions cover records in the supplied reference data.

## v7 fresh goal holdout

`--version v7a` and `--version v7b` select two 100-task, single-turn Chinese
request sets in `tasksets/odoo-goals-zh-v7a-holdout.json` and
`tasksets/odoo-goals-zh-v7b-holdout.json`. Both SHA-256 digests are pinned in
`goals/run_goals.py`. Each set has 60 ordinary requests (`F`), 25 long-tail
requests (`L`), and 15 unavailable or genuinely ambiguous requests (`U`).
The A and B prompts are different; their reference date is 2026-09-26 in
Asia/Shanghai. A served as the P8.9 development set; B was run once, as the P8.9
holdout, on 2026-09-27, and has been seen since.

## Multi-turn follow-up holdout

`--version mt-holdout` selects 40 frozen Chinese two-turn tasks in
`tasksets/odoo-goals-zh-multiturn-holdout.json`. Its prompt strings do not overlap
the 16-task `mt-dev` set. The six reply types are candidate choice (7), correction
(7), reference to the previous turn (7), change of question (6), narrowing (7),
and choice by the assistant's list number (6). The adjacent seal and the runner
pin the task file's SHA-256 digest. No model run was used to author or verify it.

For numbered replies, evaluator-only candidate checks are bound to the order the
assistant actually listed on its first turn. A zero-model parser requires a
distinct, known candidate in each numbered item; missing, repeated or mixed
items fail closed. The first-turn check separately requires a clarification with
all candidates, while the final `parts` check uses the candidate at the user's
chosen ordinal. `test_goals_multiturn_holdout.py` checks both orders and invalid
lists. The fixture facts reused by these questions were checked under the
ordinary reader with the zero-model v7a and v7b oracle verifier, without a page
walk. No model performance or browser navigation outcome is claimed.

Scoring writes one of five `outcome` values for every attempt: `correct`,
`appropriate_clarification`, `truthful_refusal`, `wrong`, or `false_claim`.
Clarification is correct only for an ambiguous task whose reply asks the user
to choose and includes the verified candidates. An unavailable task passes
only with a specific, honest limitation and no write. The aggregate report
adds:

- `trustworthiness`: correct immediate answers and truthful refusals divided
  by all nonempty, non-clarifying final replies or explicit completion claims.
  This denominator includes wrong answers that do not claim completion.
  An appropriate clarification and an empty failure are outside it, and so is a
  reply to a task the application can do that says only that this attempt was
  not done (nothing found by that name, a part not completed, the service
  unavailable); a reply asserting that the application lacks or forbids something
  stays in it. On an unavailable task every reply is in it.
- `coverage`: correct immediate answers and truthful refusals divided by all
  attempts. Appropriate clarification earns a correct outcome but is not
  immediate coverage.
- `clarification_rate`: replies that ask the user to choose or clarify divided
  by all attempts, including inappropriate requests for clarification.

Replies are read as content: Markdown emphasis is removed before any check, a
state may be given by Odoo's own label (Quotation for `draft`, Sales Order for
`sale`), a turn that ends asking the user to choose is scored as a clarification,
and the numbered choices' own statuses are not completion claims.

For these tasks, “销售单/销售订单” means confirmed orders; an unqualified “订单”
includes quotations unless its context narrows the state. Every scoped prompt
either states the intended state or is checked against that ordinary reading.
“某月开出的客户发票” means posted customer invoices, excluding drafts. A new
customer invoice handoff accepts either `/odoo/invoicing` or
`/odoo/customer-invoices` with the `New` control highlighted. Boolean answers
may use the visible field label and Yes/No, and field names may use visible or
technical spelling. Limitation words and the subject keyword may appear in
either order. Source-rule citations must cover the line that states the rule;
an outer XML container line alone is insufficient. As with earlier suites,
the citation is byte-verified and the answer must separately pass its content
check.

The zero-model oracle command verifies the ordinary reader's facts and
permissions, source bytes and rule lines, visible page and handoff targets,
and that the grader accepts each standard answer while rejecting a clear
wrong answer. It checks the fixture and judge, not model performance:

```bash
PYTHONPATH=src:examples/odoo/src:. .venv/bin/python -m evals.odoo.goals.verify_v7_holdout
PYTHONPATH=src:examples/odoo/src:. .venv/bin/python -m evals.odoo.goals.verify_v7_holdout \
  --task-file evals/odoo/tasksets/odoo-goals-zh-v7b-holdout.json
```

Before a v4 model run, `python -m evals.odoo.goals.qualification --output
.artifacts/v4-qualification.json` must pass. It independently checks all 58 answer
oracles against read-only SQL witnesses, all 58 page and handoff targets in a
browser signed in as the ordinary reader, and the 15 limitation preconditions.
The answer witnesses check fixture data, not actor ACLs; the browser walk checks
target reachability, not whether the Agent will navigate correctly. The run command
then requires `--qualification .artifacts/v4-qualification.json` and refuses a
different candidate commit, fixture fingerprint, Odoo container, mounted addon
content, or application date. It also stops if any of those change between attempts. Supply `--model-env`
and `--credentials` explicitly when running from a separate worktree. For an
isolated deployment, also supply its `--base-url`, `--page-controller-module`,
`--container`, and `--app-container`. The browser's evaluation clock is fixed to
the sealed reference date; the application clock must independently report that
same date throughout the batch. The date check detects drift but does not freeze
Odoo's clock. Do not run the relative-time holdout against a deployment reporting
any other date. The automatic answer regexes are provisional; a blind human
review of answer scope and limitation wording is required before claiming a final
success rate. A bound run may be split into lanes as long as every lane carries the same binding,
and reports reachable and limitation outcomes separately. The prior independent Odoo offline
suite still has five failures in `test_p74b_defect_paths` on both this candidate
and main; they are not counted as passing qualification.

## Source-rule holdout

`--version v6-rules` selects 40 frozen questions: visibility 8, field editability 8,
similar controls 6, refusal guards 8, field meanings 5, and absent rules 5. Truth is
bound to the deployed `18.0-20260909` tree, including its configured image suffix;
freeze date and reference date are 2026-09-25 (Asia/Shanghai). Every task's source
locations, element/method names, clauses and slice digests are in evaluator-only
truth. The adjacent seal records each verified location, the eight whole-file
hashes checked against the isolated deployment, the allowlisted source surface
digest, and the negative-rule search scope. No task truth is passed to the Agent.

`source_regex` matches **only verified citations in the visible answer**. The
interoperability format is a citation line, optionally in a Markdown quote or code
block: `relative/file.py:10-14@release sha256:<64 lowercase hexadecimal digits>`.
The digest covers the exact UTF-8 source lines, including their original line
endings. `sha256=` and a newline before the digest are also accepted. No bare path,
unverified hash, changed file, out-of-range citation or different release counts.
The evaluator reads only files listed in the sealed binding; the configured root
is never supplied by the answer. A binding mismatch fails before model dispatch.

Verified citations are normalized to `file:start-end@release` and their covered
`file:line@release` atoms. Rule checks require the exact truth lines, allowing both
a compiler's element slice and a generic retriever's enclosing block to qualify.
Limits are 512 lines per citation, 64 citations and 200,000 characters per answer.
A valid digest verifies bytes, not the semantic accuracy of an explanation; answer
keywords and source positions are separate checks, neither proves entailment.
A citation also does not prove which retrieval tool the assistant used.

Attempt JSON now includes per-part `answer_checks` and `source_checks` (null where
inapplicable), alongside the unchanged combined `achieved` field. A metadata-only
answer can pass the keyword check but cannot pass the citation check without a
verified citation. Compiled-source and generic-retrieval arms use the same oracle.
Positive answers must distinguish deployed-source rules from current data. Correct
rule text saying an operation cannot be done is not treated as assistant failure
when all answer and source checks pass. Database writes still fail every task.
Absent-rule tasks require a specific `explains_regex`, an explicit limitation and
a finished/refused turn; missing source evidence does not establish that an external
company policy does not exist. Regex grading is not a complete semantic assessment.

This change supplies the frozen questions and evaluator, not arm selection or a new
product source-rule renderer. Producers must emit the citation format above; the
current goal port does not yet declare the optional `source` capability. No three-arm
model run has been performed. Recheck the source locations without exposing prompts:

```bash
PYTHONPATH=src:examples/odoo/src:. python -m evals.odoo.goals.verify_rules \
  --source-root "$WOA_ODOO_SOURCE_ROOT" --source-release "$WOA_ODOO_SOURCE_RELEASE"
```

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

The apparatus is grouped by responsibility; its tests use the same groups:

| Package | Responsibility |
| --- | --- |
| [`domain/`](domain/) | task population, sealed task specifications, protocol, and outcome algebra |
| [`fixture/`](fixture/) | isolated Odoo stack, seed, snapshots, manifest, revision, and actor-side reads |
| [`execution/`](execution/) | attempt driver, shared harness, browser runners, approval oracle, and trace projection |
| [`provider/`](provider/) | the only model-provider gateway and the Agent's loopback relay |
| [`judging/`](judging/) | independent truth, extraction, observation typing, leak checks, and judgement |
| [`qualification/`](qualification/) | deterministic scripted programs, destructive probes, and qualification-result checks |
| [`reporting/`](reporting/) | append-only ledger, exact statistics, and schema-fixed reports |

[`tasksets/`](tasksets/) holds the development set, both seals, and the split verification.
The three root command modules remain the supported entry points: [`provision.py`](provision.py),
[`qualify.py`](qualify.py), and [`batch.py`](batch.py).

## Limitations

Every report states these, whatever its numbers:

- The provider alias is not a model snapshot, and results hold only for the recorded alias and time window. A changed model string seals a batch, but no check can prove unchanged weights.
- The fixture is one synthetic application.
- The screening gate screens; it is not a reliability proof.
- The task set is balanced by stratum only, not by locale, actor or turn depth, and two observations per task support no per-task or per-action rate.
- The provider offers no usage export, so the absence of calls outside the apparatus on the same credential is unproven.

The held-out protections above are enforced by the batch entry and by the git history of the seal and the candidate. No external review record or freeze chain binds them.
