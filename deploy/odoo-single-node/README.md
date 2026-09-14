# Odoo single-node pilot deployment

This is the supported reference deployment for a private, single-organization pilot. It runs pinned Odoo 18, PostgreSQL 17, the server Agent, and Caddy on one Linux Docker Compose host. Odoo's own login establishes the browser identity; the browser carries a short-lived signed assertion to the Agent and never supplies an actor id, API key, model key, permission list, or approval. This is not a multi-tenant cloud topology.

## Prerequisites and boundaries

Use a current Docker Engine with Compose v2, Python 3.12, Node.js, and a clean checkout. Allocate a private Linux host with persistent Docker volumes and restrict shell and Docker access to operators. The public listener binds to loopback by default; put it behind an organization-controlled ingress before allowing remote clients. The built-in Caddy certificate authority is suitable for a controlled pilot after its root certificate is installed on managed clients. Replace it with the organization's certificate at a public hostname.

The deployment uses one delegated Odoo API key per user. An API key is never shared between users. The application's access rules remain authoritative, while the delegation manifest can narrow the typed-tool scopes further. Removing a grant does not manufacture permission revocation inside an already running task: revoke the key in Odoo and log the user out, then restart the Agent if immediate closure of every issued browser capability is required.

## Prepare the deployment

Fetch and verify the PageController release pinned by the repository before building the Odoo image. The generated third-party files are ignored by Git and included in the local Docker build context.

```bash
PYTHONPATH=src:examples/odoo/src .venv/bin/python -m web_operation_agent_odoo.deploy assets
python deploy/odoo-single-node/prepare.py --private-dir .private/odoo-single-node
```

Write the provider settings into `.private/odoo-single-node/model.env`. The file accepts `AGENT_OPENAI_BASE_URL`, `AGENT_OPENAI_API_KEY`, `AGENT_OPENAI_MODEL`, and optional `AGENT_PROVIDER`. Keep its mode at `0600`. For a deterministic qualification that does not contact a model, set `WOA_SCRIPTED=1` instead.

Choose values explicitly so this stack cannot collide with a developer fixture. Keep the same values for every later Compose and operations command.

```bash
export WOA_PROJECT_NAME=woa-odoo-pilot
export WOA_PRIVATE_DIR="$PWD/.private/odoo-single-node"
export WOA_PUBLIC_ORIGIN=https://localhost:18443
export WOA_HTTPS_PORT=18443
export WOA_BUILD_REVISION="$(git rev-parse HEAD)"
docker compose -f deploy/odoo-single-node/docker-compose.yml build
docker compose -f deploy/odoo-single-node/docker-compose.yml --profile setup run --rm odoo-init
docker compose -f deploy/odoo-single-node/docker-compose.yml up -d db odoo
```

Open `https://localhost:18443/web/login`, use the generated admin password in `.private/odoo-single-node/odoo-admin-login-password`, create the pilot users, and create one expiring API key in each user's profile. Record each numeric user id from Odoo. Save each key in an operator-only temporary JSON file with exactly this shape, then set its mode to `0600`:

```json
{"database":"woa_trial","login":"person@example.test","api_key":"REPLACE_WITH_THE_USER_KEY"}
```

Register a read-only user through the built image. This command authenticates the key against the running Odoo instance and refuses an actor mismatch before copying the credential into the private directory.

```bash
docker compose -f deploy/odoo-single-node/docker-compose.yml run --rm --no-deps --entrypoint python -v "$PWD/reader-key.json:/run/woa-register/key.json:ro" -v "$WOA_PRIVATE_DIR:/run/woa-output" agent /usr/local/lib/woa-register.py --private-dir /run/woa-output --rpc-base-url http://odoo:8069 --actor-id 9 --api-key-file /run/woa-register/key.json --scope customers:read --scope invoices:read --scope products:read --scope sale_orders:read
```

Register a writer only with the write scopes the person needs. `--allow-mutations` makes those scopes eligible; the deployment-wide `WOA_ALLOW_MUTATIONS=1` switch and a fresh in-panel approval are still required before dispatch.

```bash
docker compose -f deploy/odoo-single-node/docker-compose.yml run --rm --no-deps --entrypoint python -v "$PWD/writer-key.json:/run/woa-register/key.json:ro" -v "$WOA_PRIVATE_DIR:/run/woa-output" agent /usr/local/lib/woa-register.py --private-dir /run/woa-output --rpc-base-url http://odoo:8069 --actor-id 8 --api-key-file /run/woa-register/key.json --scope customers:read --scope invoices:read --scope products:read --scope sale_orders:read --scope customers:write --scope invoices:write --scope sale_orders:write --allow-mutations
```

Delete the temporary input files after registration and start the Agent. Ordinary users now log in through Odoo and see the embedded assistant without Playwright, browser profile injection, or a separate launcher.

```bash
export WOA_ALLOW_MUTATIONS=1
docker compose -f deploy/odoo-single-node/docker-compose.yml up -d
curl -sk "$WOA_PUBLIC_ORIGIN/assistant/healthz"
curl -sk "$WOA_PUBLIC_ORIGIN/assistant/readyz"
```

`healthz` reports process liveness. `readyz` reports bounded session and provider admission state without user content. The internal telemetry snapshot is available only inside the Agent container:

```bash
docker compose -f deploy/odoo-single-node/docker-compose.yml exec -T agent python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8767/internal/metrics').read().decode())"
```

The default limits are 10 browser sessions, 3 active provider calls globally, and 2 active provider calls per actor. An eleventh session receives a typed `429 assistant_capacity` response, and per-actor admission prevents one person from consuming every provider slot. Change these limits only through a reviewed host configuration; the reference Compose intentionally does not turn them into unbounded environment switches.

## Observability and alerts

Set `WOA_OTEL_EXPORTER_OTLP_ENDPOINT` to the base URL of an OTLP/HTTP receiver reachable from the Agent container, such as an organization-managed or local OpenTelemetry Collector on the Compose network. The Agent sends standard JSON requests to `/v1/traces` and `/v1/metrics`. Spans cover model work, typed tools, browser round trips, answer verification, and the whole turn; metrics include model admission wait, model call count, and turn duration. Export is asynchronous and best effort, so a collector outage cannot change task or write semantics.

Set `WOA_ALERT_WEBHOOK_URL` to a private operational receiver. Alerts are content-free and cover three consecutive provider failures, an indeterminate write, and backup or restore failure. Set `WOA_COST_ALERT_CNY` to a positive per-run threshold for known model cost. Labels and alert bodies contain no user goal, record field, answer, raw identity, credential, or tenant name. A metric proves that the application emitted a signal; it does not replace the audit ledger or business postcondition verification.

## Backup and restore

Run a deployment backup at least daily to meet the pilot RPO target of 24 hours. The operation stops Agent and Odoo writers, uses SQLite's online backup API for session, mutation, and conversation state, creates an MVCC-consistent PostgreSQL dump, archives Odoo attachments, verifies every digest, and restarts the services. It excludes API keys, model credentials, browser capabilities, leases, and approvals. Back up the private credential directory separately with the organization's secret manager.

```bash
deploy/odoo-single-node/operations.py --project "$WOA_PROJECT_NAME" backup --destination .private/odoo-backups/2026-09-14
deploy/odoo-single-node/operations.py --project "$WOA_PROJECT_NAME" verify .private/odoo-backups/2026-09-14
```

Restore is destructive to the explicitly named isolated Compose project and therefore requires the same project name twice. Verify business writes dispatched after the backup timestamp before restoring: an old journal is evidence and must never cause an action to run again. The restore recreates the application database and attachment volume, restores durable Agent records, discards execution authority, and issues no model or tool call.

```bash
deploy/odoo-single-node/operations.py --project "$WOA_PROJECT_NAME" restore .private/odoo-backups/2026-09-14 --confirm-project "$WOA_PROJECT_NAME"
```

The measured qualification on the reference host restored the database and attachment plus Agent state in 19.34 seconds, below the 30-minute pilot target. A direct Agent restart also recovered below the 5-minute target, preserved the stable session identity and conversation record, issued a new capability, and rejected the old capability. These are one-host qualification observations, not universal latency guarantees.

## Upgrade and rollback

Take and verify a backup before every upgrade. Preserve the current image under an immutable local or registry tag, build the candidate with the exact source revision, inspect the resulting digest, recreate only the Agent, and run the health and ordinary-browser checks. Database or addon migrations require their own Odoo-tested rollback plan; retagging an image does not reverse a data migration.

```bash
docker image tag "$WOA_PROJECT_NAME-agent:latest" "$WOA_PROJECT_NAME-agent:rollback-$(date +%Y%m%d)"
export WOA_BUILD_REVISION="$(git rev-parse HEAD)"
docker compose -f deploy/odoo-single-node/docker-compose.yml build agent
docker compose -f deploy/odoo-single-node/docker-compose.yml up -d --no-deps --force-recreate agent
curl -sk "$WOA_PUBLIC_ORIGIN/assistant/readyz"
```

If qualification fails before a data migration, retag the saved digest as `latest`, recreate Agent, and rerun readiness plus a read-only browser journey. The P7.3 qualification performed this exact candidate upgrade and image rollback and preserved the database and durable audit state.

## Pilot limitations

This topology is one process per service on one machine. Active model calls and browser actions are not migrated; restart recovery retains safe history and unresolved-write records but never resumes a task or approval. The built-in TLS and loopback listener need an ingress and managed certificate for remote users. Secrets are filesystem inputs rather than a cloud secret manager. Backups are operator-run and not replicated. OpenTelemetry receivers, dashboards, and notification routing are operator-owned. Multi-host scaling, cross-region recovery, enterprise SSO, automatic key rotation, and a managed connector control plane remain outside this pilot.
