---
title: Deployment & Operations
description: Online rollout with ./deploy, graceful shutdown probes, SQLite WAL backup and isolated restore, INDETERMINATE reconciliation, spend caps, OTLP telemetry, and troubleshooting.
---

This page covers online deployment and runtime operations: rollout scripts, health probes and graceful drain, SQLite consistent backups and restores, `INDETERMINATE` write reconciliation, three-tier spend limits, content-free telemetry, and troubleshooting.

## 1. Online Deployment (`./deploy`)

Hosted deployments default to `read-only` (writes, edit prefill, and user-facing source lookup disabled), with read-only HTTP method guards on static assets and proxy paths:

```bash
cp deploy.example.toml deploy.local.toml   # Set ssh_target and public_origin (git-ignored)
./deploy check                             # Read-only check of local build and remote prerequisites
./deploy                                   # Build frontend & wheel, upload release, check health, switch current
./deploy status                            # Inspect remote release.json and verify live assistant.js digest
```

## 2. Graceful Shutdown and Health Probes (`/healthz` & `/readyz`)

`gateway.py` exposes unauthenticated probes for container orchestrators and reverse proxies:

| Endpoint | Healthy response | Behavior after `SIGTERM` drain starts |
| --- | --- | --- |
| `GET /healthz` | `200` `{"ok": true, "protocol": 1, "schema_version": 1}` | Process liveness probe |
| `GET /readyz` | `200` `{"ready": true, "accepting_sessions": true, "session_count": N}` | Switches to HTTP `503` (`ready: false`, `accepting_sessions: false`), stops accepting new turns, and waits for in-flight dispatch/readback up to `shutdown_drain_timeout_s` |

## 3. SQLite Consistent Backup and Isolated Restore

Session stores, mutation journals, and spend ledgers use WAL-mode SQLite. **Do not `cp` live `-wal` database files directly.** Use `web_operation_agent.composition.operations` (`src/web_operation_agent/composition/operations.py`):

- **Backup bundle:** Creates a `0o700` directory with `manifest.json` (`BACKUP_SCHEMA_VERSION = 1`), SHA-256 digests for all database files, and `PRAGMA quick_check` validation.
- **No credentials:** Bundles contain runtime ledgers and digests only—no model keys, cookies, or replayable tokens.
- **Isolated restore:** `restore` writes only to a fresh target directory (never overwriting a live runtime directory) and requires `--writer-isolated` to confirm writer isolation. Restored `mutation_journal` records serve as reconciliation evidence only, never replay authority.

```bash
.venv/bin/python -m web_operation_agent.composition.operations inspect --help
.venv/bin/python -m web_operation_agent.composition.operations backup --help
.venv/bin/python -m web_operation_agent.composition.operations verify --help
.venv/bin/python -m web_operation_agent.composition.operations restore --writer-isolated --help
```

## 4. Reconciling `INDETERMINATE` Writes

When a write records `mutation_claimed` (`dispatching` or `indeterminate`) in `SQLiteMutationJournal` and the external call times out or disconnects, the framework **never retries automatically** because a single read cannot prove whether the prior backend transaction rolled back or is still committing.

Use `web_operation_agent.business.operations` (`src/web_operation_agent/business/operations.py`) or the session API to reconcile:

1. **Inspect unresolved items (`list`):** Lists each unresolved item's `proposal_digest`, last stage, captured `result_digest`, and actor digest.
2. **Read-only check (`reconcile`):** Runs a read-only check as the original actor (`dispatches: 0`, `journal_changed: False`) and reports whether human confirmation is still needed (`requires_human`).
3. **Record the resolution:**
   - **Offline/stopped-writer CLI** (dry-run by default; requires both `--apply` and `--writer-stopped` to modify the journal):
     ```bash
     .venv/bin/python -m web_operation_agent.business.operations list --help
     .venv/bin/python -m web_operation_agent.business.operations resolve --writer-stopped --help
     ```
   - **Online session API:** An authorized operator submits the finding to `POST /v1/sessions/<id>/reconciliations`, appending the outcome to the session timeline.

## 5. Three-Tier Budget and Spend Controls

`src/web_operation_agent/modeling/spend.py` and `src/web_operation_agent/execution/budget.py` enforce three independent layers:

| Layer | Component | Scope | Persistence & boundary behavior |
| --- | --- | --- | --- |
| Turn budget | `TurnBudget` | Single turn (`steps`, `seconds`) | Shared atomic step count and monotonic clock across API, SQL, and UI stages; retries count as steps |
| Daily actor allowance | `DailyAllowance` | Per day + per actor principal | Checked before dispatch; reconstructed from persisted daily ledger after process restart |
| Atomic ledger cap | `SQLiteSpendCap` + `SpendMeter` | Process/deployment spend ledger (default `turn_reserve_cny = 0.02`) | Atomically reserves minimum headroom before model calls and records actual token cost on completion |

## 6. Content-Free Telemetry and Turn Diagnostics

### OTLP Telemetry (`RuntimeObservability`)

`src/web_operation_agent/modeling/observability.py` exports structured metrics and spans via a background daemon thread (`woa-otel-export`, bounded queue size `256`) without user prompts, cookies, or business table payloads:

- Deterministic SHA-256 trace/span IDs per `run_id` with stage timings.
- Built-in counters: `turns`, `failed_turns`, `provider_failures`, `provider_deadlines` (including consecutive failure/deadline counts), `indeterminate_writes`, `stale_runs_reaped`, `maintenance_failures`, `exports_failed`, and `alerts_failed`.
- Configurable `otlp_endpoint`, `alert_webhook`, and `cost_alert_cny` (positive float up to `1_000_000`).

### Single-Turn Diagnostics (`TurnTrace` & `trace_replay.html`)

- **`TurnTrace` (`execution/turn_trace.py`):** Records `stage_timings`, `tool_timings`, `first_route_kind`, `stop_reason`, and `checks`, audited and rescanned at `url_decode_depth=3` before writing.
- **`trace_replay.html` (`state/traceview.py`):** Generates a static redacted replay page from the append-only `SessionLedger`, projecting only `VISIBLE_EVENTS` (`approval_requested`, `approval_decided`, `tool_call`, `application_entered`, `action_receipt`, `write_permitted`, `success_claim`, `run_finished`, `product_verification`, etc.).

## 7. Troubleshooting

| Symptom | Common cause | Diagnosis | Resolution |
| --- | --- | --- | --- |
| `/bootstrap` returns `401` / `403` | Expired host login or proxy stripped auth headers | Check `start.final_statuses` and reverse proxy headers | Re-authenticate in the host app and verify header forwarding |
| `/v1/runtime/assistant.js` returns `404` (`runtime_unavailable`) | Built frontend runtime asset missing | Inspect JSON error `code` | Run `python scripts/build_runtime.py` before starting the gateway |
| Action or approval returns `409` | Duplicate tab triggered Web Locks rotation, or `approval_id` replayed | Inspect `woa.assistant.tab.v1` and latest `/timeline` events | Refresh the panel conversation or confirm a newly generated approval card |
| Turn ends early with `step_budget` or `wall_budget` | Broad query caused repeated pagination or excessive UI exploration | Inspect `TurnTrace` `stage_timings` and `stop_reason` | Narrow filters, use summary/SQL tools, or raise configured budget |
| `INDETERMINATE` entry in journal | Timeout or disconnect after `mutation_claimed` | Run `business.operations list` | Verify outcome in the target system, then run `resolve --writer-stopped --apply` or `POST /reconciliations` |
| `/readyz` returns `503` | Process draining on shutdown or upstream readiness failed | Run `curl -i /readyz` | Wait for drain completion or check model/backend connectivity |
