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
