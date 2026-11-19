---
title: Operations & Observability
description: SQLite operational backup and isolated restore, reconciling indeterminate writes, 3-tier spend governance, content-free OTLP telemetry, and troubleshooting.
---

> **TL;DR:** Production instances isolate persistent state in SQLite (`WAL`), support online checksummed backups with writer-isolated restores, never auto-retry `INDETERMINATE` writes without operator reconciliation, enforce 3-tier turn/daily/ledger spend caps, and stream content-free OpenTelemetry spans and counters without exposing prompts or row data.

[简体中文](../zh-cn/security/operations-and-observability.md)

## 1. Consistent SQLite backup, verify, and restore

`web_operation_agent.composition.operations` (`src/web_operation_agent/composition/operations.py`) packages consistent snapshots of local operational SQLite stores (sessions, mutation journals, spend caps) into self-contained verification bundles:

- **Bundle format:** Writes a `0o700` bundle directory with `manifest.json` (`BACKUP_SCHEMA_VERSION = 1`), SHA-256 checksums for every database file, and SQLite `PRAGMA quick_check` validation.
- **No secrets in backups:** Bundles contain operational state and digests only—never model keys, cookies, or execution tokens.
- **Isolated restore by design:** Restoring creates a **new** directory and refuses to overwrite a live deployment directory in place, requiring explicit attestation that writers are stopped so old mutation journals are inspected for reconciliation rather than replayed.

```bash
# Inspect current operational database status and integrity
.venv/bin/python -m web_operation_agent.composition.operations inspect --help

# Create a consistent online backup bundle and verify its SHA-256 manifest
.venv/bin/python -m web_operation_agent.composition.operations backup --help
.venv/bin/python -m web_operation_agent.composition.operations verify --help

# Restore into a fresh 0o700 target directory (only after stopping writers)
.venv/bin/python -m web_operation_agent.composition.operations restore --writer-isolated --help
```

*(Pass `--help` on any subcommand above to view exact database-dir / bundle-dir paths for your deployment layout.)*

---

## 2. Reconciling `INDETERMINATE` writes

When a single-flight write has already written `mutation_claimed` into `SQLiteMutationJournal` (`state` = `"dispatching"` or `"indeterminate"`) and the upstream network request times out or drops, PageHelm **stops and never retries automatically**: a finite read cannot prove that the earlier server transaction aborted safely.

Inspect and resolve pending intents via `web_operation_agent.business.operations` (`src/web_operation_agent/business/operations.py`) or the live HTTP reconciliation endpoint:

1. **List unresolved journal intents (`list`):** Displays each pending `proposal_digest`, reached execution phase, captured `result_digest`, and original actor identity digest so an operator does not need to reconstruct CAS states by hand.
2. **Verify upstream outcome under the original actor (`reconcile`):** Performs a read-only actor-bound inspection returning `outcome`, `requires_human`, `dispatches: 0`, and `journal_changed: False`.
3. **Commit operator resolution:**
   - **Offline / stopped writer CLI:**
     ```bash
     .venv/bin/python -m web_operation_agent.business.operations list --help
     .venv/bin/python -m web_operation_agent.business.operations resolve --writer-stopped --help
     ```
     (`resolve` runs in dry-run preview mode unless `--apply` is explicitly supplied, and requires `--writer-stopped` attestation.)
   - **Active session HTTP API:** Submit an authenticated operator decision to `POST /v1/sessions/<id>/reconciliations` so the timeline records the resolved outcome alongside the original audit chain.

---

## 3. Graceful shutdown and health probes

The gateway exposes unauthenticated operational probes designed for container orchestrators and reverse proxies (`gateway.py`):

| Endpoint | Healthy response | Behavior during drain / shutdown |
| --- | --- | --- |
| `GET /healthz` | `200` `{"ok": true, "protocol": 1, "schema_version": 1}` | Process liveness check |
| `GET /readyz` | `200` `{"ready": true, "accepting_sessions": true, "session_count": N}` | Transitions to HTTP `503` (`"ready": false`, `"accepting_sessions": false`) on shutdown / SIGTERM drain so load balancers stop routing new sessions immediately while active turns finish or record cancellation cleanly |

---

## 4. Three-tier cost and quota governance

`src/web_operation_agent/modeling/spend.py` and `src/web_operation_agent/execution/budget.py` enforce layered spending limits:

| Tier | Class | Scope | Persistence & Edge Behavior |
| --- | --- | --- | --- |
| **1. Turn execution cap** | `TurnBudget` | Single turn (`steps`, `seconds`) | Shared atomic request counter + monotonic clock across read/SQL/browser stages; retries consume steps |
| **2. Per-actor daily cap** | `DailyAllowance` | Per deployment principal key per calendar day | Checked pre-dispatch; tally is **rebuilt from persisted ledger rows at startup** so process restarts never reset daily quotas |
| **3. Persistent reserve cap** | `SQLiteSpendCap` + `SpendMeter` | Deployment/run spend ledger (`turn_reserve_cny=0.02` default) | Reserves minimum turn cost before model dispatch and records exact token/CNY accounting atomically |

---

## 5. Content-free OTLP telemetry (`RuntimeObservability`)

`RuntimeObservability` (`src/web_operation_agent/modeling/observability.py`) ships operational telemetry without ever emitting raw user prompts, session handoff tokens, or row cell data:

- **Background OTLP exporter (`woa-otel-export`):** Daemon thread with a bounded 256-item queue exporting SHA-256 trace/span IDs per `run_id`, timestamps, and structural counters:
  - `turns`, `failed_turns`
  - `provider_failures`, `provider_deadlines` (also tracks failure/deadline streaks)
  - `indeterminate_writes`, `stale_runs_reaped`, `maintenance_failures`
  - `exports_failed`, `alerts_failed`
- **Cost & anomaly alerting:** Configure `otlp_endpoint`, optional `alert_webhook`, and `cost_alert_cny` threshold (bounded positive float up to `1_000_000`) to notify on-call channels cleanly.

---

## 6. Per-turn diagnostics: `TurnTrace` and `trace_replay.html`

- **`TurnTrace` (`execution/turn_trace.py`):** Captures structural per-turn latency diagnostics (`stage_timings` via `record_stage`, per-tool durations and `error_code` via `record_tool_timing`, `first_route_kind`, `stop_reason`, and programmatic `checks`), alongside accepted model messages. Before persistence, records pass provider egress verification and credential pattern scanning (`url_decode_depth=3`).
- **`trace_replay.html` (`state/traceview.py`):** Compiles a self-contained, redacted timeline replay from `SessionLedger` filtering strictly to `VISIBLE_EVENTS` (`approval_requested`, `approval_decided`, `tool_call`, `application_entered`, `action_receipt`, `write_permitted`, `success_claim`, `run_finished`, `product_verification`, etc.).

---

## 7. Operational troubleshooting matrix

| Symptom | Likely root cause | Diagnostic check | Resolution |
| --- | --- | --- | --- |
| Bootstrap fails with HTTP `401` / `403` | Application actor login expired or reverse proxy stripped identity headers | Check `start.final_statuses` / upstream auth header forwarding | Refresh upstream application login; verify reverse-proxy header pass-through |
| `404` `runtime_unavailable` on `/woa/assistant.js` | Packaged installation missing built JS/CSS assets | Check `GET /woa/assistant.js` status message | Run `python scripts/build_runtime.py` before launching gateway |
| `409` conflict on tab action or approval | Duplicate tab rotation triggered Web Locks reset, or single-use `approval_id` replayed | Inspect tab `woa.assistant.tab.v1` / panel timeline event | Refresh panel turn cleanly; request fresh approval card if proposal changed |
| Turn stops with `step_budget` / `wall_budget` | Overly broad query triggered many pagination rounds or long UI exploration | Inspect `TurnTrace` `stage_timings` and `stop_reason` | Narrow date/entity filters, register typed summary/SQL read, or adjust `TurnBudget` |
| `INDETERMINATE` write state in panel/journal | Network drop after single-flight `mutation_claimed` | Run `business.operations list` | Verify upstream record manually, then run `resolve --writer-stopped --apply` or `POST /reconciliations` |
| `/readyz` returns `503` | Process draining during shutdown or readiness hook reports unhealthy | `curl -i /readyz` | Wait for active drain completion or inspect upstream dependency readiness |

---

## Next steps

- Check local startup, configuration keys, and CI gates in [5-Min Quickstart & Ops](../integration/quickstart.md).
- Inspect exact `/v1/sessions/*` JSON contracts and environment variables in [Protocol & Configuration Reference](../reference/protocol-and-configuration.md).
