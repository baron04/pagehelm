# Twenty deterministic evaluation foundation

This directory contains a small evaluator for the separately provisioned, synthetic
Twenty CRM fixture. It is outside `src/`, is not packaged with the product, and must run
as a separate process. Product and Agent code must never import it.

The evaluator currently covers only three Companies tasks:

- `query`: the response must list every active company name and domain, and business
  state must remain unchanged;
- `open`: the final page must show the one company selected by its baseline ID, while
  business state remains unchanged; and
- `create`: exactly one requested company must be added, existing companies, people,
  and opportunities must remain unchanged, the write must have one acknowledged
  same-actor readback, and the created record page may be required.

This is fixture infrastructure, not a complete benchmark. It does not measure planning
quality, latency, model cost, source-retrieval benefit, or PageAgent performance.

## Frozen qualification task sets

[`tasksets/twenty-companies-development-v1.json`](tasksets/twenty-companies-development-v1.json) is a public six-case development set for the three supported Companies task shapes. The twelve-case held-out test set stays outside public Git, while [`tasksets/twenty-companies-test-v1.seal.json`](tasksets/twenty-companies-test-v1.seal.json) commits its opaque task IDs, nonce-salted per-task digests, template-family digests, counts, release, and complete-set digest. The nonce remains inside the private full test set until unsealing, which prevents practical dictionary guessing of short prompts from their public task digests. [`tasksets/twenty-companies-split-v1.json`](tasksets/twenty-companies-split-v1.json) records that the development and test seals use disjoint task IDs, task digests, and template-family digests.

These 6 development cases and 12 held-out cases qualify the task/evaluator pipeline; they are not statistically powered and do not represent the planned breadth benchmark. Rephrased prompts and changed company values do not create independent workflow units, so these files cannot support a general success-rate, PageAgent, or source-benefit claim.

Seal and verify a task set before using it:

```bash
python -m evals.twenty seal-taskset \
  --taskset evals/twenty/tasksets/twenty-companies-development-v1.json \
  --output evals/twenty/tasksets/twenty-companies-development-v1.seal.json

python -m evals.twenty verify-taskset \
  --taskset evals/twenty/tasksets/twenty-companies-development-v1.json \
  --seal evals/twenty/tasksets/twenty-companies-development-v1.seal.json
```

Before a run, bind an open task to the exact frozen baseline and emit an evaluator task without exposing database truth to the Agent:

```bash
python -m evals.twenty instantiate-task \
  --taskset evals/twenty/tasksets/twenty-companies-development-v1.json \
  --task-id dev-open-en-1 \
  --baseline .artifacts/evals/twenty/before.json \
  --output .artifacts/evals/twenty/case.json
```

The execution case contains the user prompt and deterministic evaluator input. Keep the full held-out task set, execution cases, provider payloads, database dumps, browser state, and raw traces in ignored private storage. Product code must receive only the prompt and ordinary application capabilities; it must never read task-set metadata, evaluator input, seals, baseline state, or prior outcomes.

## Machine-checked run protocol

[`protocol.py`](protocol.py) validates the preregistration boundary before a paired run. A draft must bind the exact development/test seals and split verification; application, browser, fixture/reset, evaluator, harness, policy and implementation digests; provider sampling and token limits; a common per-case budget with zero automatic retries or user interventions; complete source exclusions; five concrete arms; three comparison-specific eligibility lists; and fail-fast stop rules.

The five configurations are official PageAgent browser-only, Web Operation Agent browser-only, PageAgentCore with the shared Skill/API contract, Web Operation Agent with that same contract and SourceProvider off, and the identical Web Operation Agent configuration with SourceProvider on. The Source OFF configuration is reused in both the Skill/API and source comparisons, so an unrecorded implementation difference cannot masquerade as a source effect. The validator also requires the two Skill/API systems to receive equal Skill and tool-catalog digests and requires SourceProvider configuration to be the only H3 difference.

Freezing derives a deterministic, pair-order-balanced schedule from a preregistered 128-bit seed and seals the canonical protocol. Every held-out task must have one eligibility row, every scheduled pair must appear exactly once, and source eligibility must agree with a pre-outcome static-knowledge reason. The code can prove structural consistency; a reviewer must still judge whether task templates and source-eligibility reasons represent the intended business population.

```bash
python -m evals.twenty freeze-protocol \
  --protocol protocol-draft.json \
  --development-seal evals/twenty/tasksets/twenty-companies-development-v1.seal.json \
  --test-seal evals/twenty/tasksets/twenty-companies-test-v1.seal.json \
  --split-verification evals/twenty/tasksets/twenty-companies-split-v1.json \
  --output protocol-frozen.json

python -m evals.twenty verify-protocol \
  --protocol protocol-frozen.json \
  --development-seal evals/twenty/tasksets/twenty-companies-development-v1.seal.json \
  --test-seal evals/twenty/tasksets/twenty-companies-test-v1.seal.json \
  --split-verification evals/twenty/tasksets/twenty-companies-split-v1.json
```

The repository currently ships the validator, not a frozen formal-run protocol. The official PageAgent bundle, both read-only runners, the common schedule driver and the public report generator are qualified without a model call; the provider snapshot policy, source allowlist and remaining environment manifests must still be qualified and hashed before creating that artifact. Model runs performed before those fields are frozen remain exploratory and cannot support benchmark claims.

## Shared outcome and trial projection

[`outcomes.py`](outcomes.py) is the common failure classifier for both engines. Model-call limits, deadlines, invalid tool arguments, route refusal and cancellation remain scorable engine outcomes. Stable identity/authentication codes become `credential_or_identity`; provider failures become `provider_outage`; API, response-contract and browser-transport failures become `transport_contract`. Web Operation Agent writes the same typed business code to its private audit event that the PageAgent Business Host returns, so the classifier does not inspect exception text.

[`trial.py`](trial.py) converts an engine runner's bounded `trial` projection into the canonical record validated by [`records.py`](records.py). It binds the artifact to the scheduled task, digest, arm and engine, then copies only accounting, action counts, safety counters and evidence digests. The same projector and append-only ledger cover PageAgent and Web Operation Agent artifacts across a complete frozen schedule.

[`evidence.py`](evidence.py) is the one evidence contract both engines use. It validates an execution case, projects the part of it a runner artifact may carry, and builds completed evidence: a query answer must be one strict JSON object that restates exactly the companies and completeness its own business results disclosed, and open evidence requires a route disclosed by a current business result, a server permit for that exact route, and the record's name and domain visible on the final rendered page. It never repairs prose, Markdown, omissions or duplicate rows, never reads database truth, and never accepts a model's tool arguments in place of an observed fact.

## Common schedule driver

[`driver.py`](driver.py) executes one frozen protocol serially. For each scheduled pair it captures the before state, instantiates the task against that baseline, calls the arm's runner, captures the after state, evaluates with the independent evaluator, projects the common trial record and appends it to the batch ledger. It cannot select, reorder, parallelize or silently retry a trial: it resumes only at the exact next ledger position, requires the second arm to start from the first arm's baseline digest, refuses an arm outside the frozen protocol or without a bound runner, and stops as soon as the batch becomes terminal. A create task requires an explicit restore whose claimed `PASS` is checked against a fresh captured baseline. A failed restore is recorded as `fixture_reset_mismatch` and seals the batch; a read-only mutation is recorded as a safety event and stops it. An interrupted trial is never recorded as completed, because the record is written only after the evaluator has scored it.

The driver contains no PageAgent or Web Operation Agent special case. An engine appears only as a runner adapter that returns the shared artifact. The projector requires the artifact's engine release, capability profile and source mode to match the scheduled arm, so a runner configured for one arm cannot be counted under another.

## Public redacted report

[`report.py`](report.py) recomputes a schema-fixed public report from a verified ledger and its frozen protocol, both re-verified on every call. It reports sample counts, `PASS`/`FAIL`/`INDETERMINATE`/`INVALID` tallies, per-arm outcome and terminal-reason counts, page/business action and model-call aggregates, token and cost completeness, safety counters and the batch stop reason. It emits no prompt, company data, per-trial timing, request ID, run ID, credential, raw trace or held-out task content.

The report labels its own purpose. A batch smaller than its schedule is `apparatus_qualification`; a complete batch with fewer than thirty trials per arm is `development_pilot`; only a larger complete batch is `formal_benchmark`. `comparative_claim_supported` is always false, and `verify_report` refuses a report that sets it. These artifacts cannot support a success rate, a PageAgent comparison, a source-retrieval effect, or a no-gain conclusion.

## Append-only batch ledger

[`records.py`](records.py) defines the common trial record that every compared runner must emit. Each record binds the protocol, held-out task digest, scheduled pair and arm; UTC interval; evaluator status and digest; terminal reason; provider model snapshots, request-ID evidence, token/cost completeness; model, page, business, approval, retry and intervention counts; four safety counters; and digests for the private evidence slices. Valid trials require every evidence digest and at least one accounted provider call.

The JSONL batch ledger is created with mode `0600`, opened without following a final symlink, protected by a POSIX file lock, and linked by SHA-256 entry digests. It accepts only the next trial in the frozen pair schedule, forbids duplicate run IDs and overlapping trials, checks the pair time window and reported model snapshot, and validates the prospective entry before writing. An apparatus-invalid record seals the batch; a valid safety event stops it; a valid product failure remains in the batch and cannot be retried away.

Usage-unavailable provider calls are counted explicitly. Their known token and cost subset remains visible, while `total_cost_cny` is required to be `null`; zero cannot stand in for unknown spend. The batch summary follows the same rule.

Initialize the private ledger with an explicit UTC timestamp, then append one runner-produced record at a time and verify the full chain:

```bash
python -m evals.twenty init-batch \
  --protocol protocol-frozen.json \
  --development-seal development.seal.json \
  --test-seal test.seal.json \
  --split-verification split.json \
  --ledger .artifacts/evals/twenty/batch.jsonl \
  --batch-id twenty-benchmark-v1 \
  --created-at 2026-09-09T00:00:00+00:00

python -m evals.twenty append-trial \
  --protocol protocol-frozen.json \
  --development-seal development.seal.json \
  --test-seal test.seal.json \
  --split-verification split.json \
  --ledger .artifacts/evals/twenty/batch.jsonl \
  --record trial.json

python -m evals.twenty verify-batch \
  --protocol protocol-frozen.json \
  --development-seal development.seal.json \
  --test-seal test.seal.json \
  --split-verification split.json \
  --ledger .artifacts/evals/twenty/batch.jsonl
```

The ledger makes omission, reordering and local tampering detectable; it is not a digital signature or an external transparency service. Keep the raw ledger private because its timing and operational metadata may still be sensitive. Public evidence should be generated later through a schema-fixed redaction and recomputation step.

## Qualify a fixture

Before collecting a trial, run `qualify` with the exact synthetic fixture identity. The command captures the business projection, creates an in-memory schema dump, captures again, and returns `READY` only when the two digests match. Its output contains fixture identity, counts, the baseline digest, and snapshot metadata; it does not contain people or opportunity rows.

```bash
python -m evals.twenty qualify \
  --container twenty-db-1 \
  --database default \
  --db-user postgres \
  --workspace-schema workspace_example \
  --output .artifacts/evals/twenty/qualification.json
```

Qualification proves that the explicitly named, already provisioned Twenty database can be observed and can produce a trial-reset dump at that moment. It does not bootstrap Twenty into an empty PostgreSQL server. The workspace schema references extensions and functions installed outside that schema, so use a full database provisioning procedure to create a new fixture.

## Trust boundary

`baseline` and `capture` invoke `psql` inside one explicitly named local container. The
caller must supply the database, PostgreSQL user, and exact Twenty workspace schema;
the evaluator never selects the first available workspace. Persisted projections contain
only the company fields needed for the three checks. People and opportunities are kept as
row counts and SHA-256 change guards, so their raw rows do not enter artifacts.

The commands accept no password or token argument. Configure the isolated container
itself for local database access. Do not use this tool with production or customer data.

## Capture and evaluate

Run commands from the repository root. Replace the example identity with the synthetic
fixture's actual values:

```bash
python -m evals.twenty baseline \
  --container twenty-db-1 \
  --database default \
  --db-user postgres \
  --workspace-schema workspace_example \
  --output .artifacts/evals/twenty/before.json
```

After the product process has finished, capture a fresh observation with the same
identity:

```bash
python -m evals.twenty capture \
  --container twenty-db-1 \
  --database default \
  --db-user postgres \
  --workspace-schema workspace_example \
  --output .artifacts/evals/twenty/after.json
```

Evaluation is file-only and makes no database or network call:

```bash
python -m evals.twenty evaluate \
  --task task.json \
  --before .artifacts/evals/twenty/before.json \
  --after .artifacts/evals/twenty/after.json \
  --evidence evidence.json \
  --output .artifacts/evals/twenty/result.json
```

Task files have `artifact_version: 1`. A query task has `kind: "query"`. An open task
also has `company_id`. A create task also has exact `name`, bare `domain`, and a Boolean
`require_open`.

Evidence also has `artifact_version: 1` and an explicit `execution_status` of
`completed`, `failed`, or `indeterminate`. A `failed` execution is a scored failure;
`indeterminate` is reserved for an uncertain action outcome. Completed query evidence
supplies a Boolean `complete` and
`companies` name/domain pairs. Completed open evidence supplies a `page` with `path`,
`name`, and `domain`. Completed create evidence supplies this receipt:

```json
{
  "artifact_version": 1,
  "execution_status": "completed",
  "write_receipt": {
    "dispatch_count": 1,
    "acknowledged": true,
    "same_actor_readback": true
  }
}
```

When `require_open` is true, create evidence also supplies the same `page` shape used by
the open task. This evidence must be produced from the public answer, page observation,
and audit receipts by a harness outside the Agent. Tool arguments or model assertions are
not substitutes for observed evidence.

The result is one of:

- `PASS`: all required postconditions and forbidden-change checks passed;
- `FAIL`: usable evidence proved at least one check false;
- `INDETERMINATE`: the final state or write outcome remains uncertain; or
- `INVALID`: task, identity, baseline, digest, or evidence cannot support the test.

An indeterminate write stays indeterminate even when the database delta looks correct.
A deterministic product failure is `FAIL`; it cannot invalidate its own trial. Failure to
capture the post-run database state is `INDETERMINATE`, while an unavailable baseline makes
the trial `INVALID` because no expected state was frozen.

## Explicit recovery for write cases

Before allowing a write case, create a baseline, copy its exact `digest`, and prepare a
PostgreSQL custom dump plus recovery record:

```bash
python -m evals.twenty snapshot \
  --container twenty-db-1 \
  --database default \
  --db-user postgres \
  --workspace-schema workspace_example \
  --baseline .artifacts/evals/twenty/before.json \
  --expected-digest 'sha256:…' \
  --snapshot .artifacts/evals/twenty/baseline.dump \
  --record .artifacts/evals/twenty/recovery.json
```

The command checks the live projection before and after `pg_dump`, limits the dump to the
explicit synthetic workspace schema, writes it with
mode `0600`, and atomically seals a record to the explicit fixture identity, baseline
digest, dump hash, and dump size. If the application can write concurrently, quiesce it
before this command and keep it quiesced until the write case starts.

Restore requires the explicit dump, recovery record, and expected digest:

```bash
python -m evals.twenty restore \
  --container twenty-db-1 \
  --database default \
  --db-user postgres \
  --workspace-schema workspace_example \
  --expected-digest 'sha256:…' \
  --snapshot .artifacts/evals/twenty/baseline.dump \
  --record .artifacts/evals/twenty/recovery.json \
  --output .artifacts/evals/twenty/restore.json
```

Before `pg_restore` starts, the recovery record is atomically changed to `RESTORING`.
`pg_restore` is also explicitly limited to that workspace schema; it does not restore the
whole database or another workspace.
A command failure becomes `INDETERMINATE` and is never retried automatically. A successful
command is `PASS` only after a fresh projection matches the expected baseline digest.
The fixture owner remains responsible for stopping application writers and checking application health; this foundation deliberately does not own the product or container lifecycle. The schema-only snapshot is bound to the exact fixture identity and is intended to reset trials inside that provisioned database. It is not a portable Twenty installation backup.

## Offline tests

The tests inject a fake command runner and do not launch Docker or read external data:

```bash
python -m unittest discover -s evals/twenty/tests -v
```
