---
title: Execution & Browser Runtime
description: Shared TurnBudget, concurrent read prefetch with deterministic commit, Decimal summaries, TurnTables SQL, and browser settling.
---

Each user turn runs inside a shared-budget loop (`ModelDrivenTurnExecutor` + `TurnHarness` + `TurnBudget`). Read calls can execute concurrently and commit in deterministic order on the main thread, numeric aggregations run in deterministic `Decimal` or temporary read-only SQL engines, and browser actions are bounded by snapshot caps and noise-filtered settle checks.

## 1. Turn Orchestration and Shared Budget (`TurnBudget`)

```text
User task input
   |
   v
TurnHarness (session binding, action fences, approval monitor, tracing)
   |
   +--> TurnBudget(steps, seconds)  <-- single atomic step counter & monotonic clock
   |       - every model call (including retries) consumes 1 step
   |       - switching between API reads, SQL, and UI actions does not reset budget
   |
   v
ModelDrivenTurnExecutor loop
   |---> concurrent read-only workers ----> ordered main-thread commit (t1, ev_*)
   |---> guarded browser UI actions ------> PageSettleWatch (80ms quiet / 2s max)
   |---> finish(table_ref=...) -----------> Shadow DOM <DataTable>
   |---> finish(status="need_input", options=[...]) --> clickable disambiguation pills
```

`TurnBudget` (`src/web_operation_agent/execution/budget.py`) enforces one shared step limit and wall-clock deadline across the turn (excluding human approval wait time):

- Before each model call, `TurnBudget.reserve()` checks cancellation, remaining seconds, and remaining steps.
- Read-tool checkpoints (`stopped_read` in `data_tools.py`) read the same `TurnBudget` and stop cleanly on `user_cancelled`, `step_budget`, `token_budget`, or `wall_clock_budget`.

## 2. Concurrent Reads with Deterministic Main-Thread Commit

When a model issues parallel read calls in one step or an adapter fetches paginated slices concurrently, execution follows "concurrent prefetch, ordered single-thread commit":

- Workers perform network requests, check response limits, and prepare redactions without mutating shared `TurnTables` or receipt stores directly.
- After workers finish, the main thread registers table handles (`t1`, `t2`, ...) and evidence receipts (`ev_<hex12>_<n>`) in strict call order so traces and regression tests stay reproducible.

## 3. Server-Side Deterministic Summaries (`read_summary.py`)

`src/web_operation_agent/execution/read_summary.py` runs closed-set `Decimal` aggregations on same-actor reads instead of relying on model arithmetic or UI pagination:

| Constraint or capability | Implemented behavior |
| --- | --- |
| Row and output caps | `MAX_ROWS = 5000`, `MAX_RESULT_CHARS = 100_000` per summary |
| Supported column types | `text`, `number`, `boolean`, `date`, `datetime`, `relation` |
| Filter operators | `=`, `!=`, `contains`, `prefix`, `in`, `>`, `>=`, `<=`, `<`, `between`, `empty`, `not_empty` |
| Measures | `count`, `count_distinct`, `sum`, `min`, `max` (exact `Decimal`; use `TurnTables` SQL for `AVG` or joins) |
| Currency/unit safety | Unverified amount columns carry `SummaryColumn.unverified_unit=True` and render raw values without guessing units |
| Truncation guard | When upstream sets `truncated=True`, partial slices cannot be reported as complete totals |

## 4. Per-Turn Temporary SQL (`TurnTables`) and `<DataTable>`

For joins, rankings, averages (`AVG`), or secondary slicing over rows already fetched in the current turn, core provides `TurnTables` (`src/web_operation_agent/execution/turn_tables.py`, automatically wired when `build_declarative_profile(..., enable_sql=True)` is used):

- **Isolation and lifecycle:** Opens `sqlite3.connect(":memory:", cached_statements=0)` with `SQLITE_LIMIT_ATTACHED = 0` and releases immediately on `TurnTables.close()` at turn end.
- **Bounds:** See [Configuration & Protocol](../reference/protocol-and-configuration.md) (36 tables/turn, 5000 rows/table, 128 columns, 200-row / 14000-char model preview, 1.5s query timeout).
- **Read-only SQL allowlist:** Permits only a single `SELECT` or `WITH` statement (others return `sql_refused`), strips comments while preserving string literals, and allowlists pure functions (`abs`, `avg`, `coalesce`, `count`, `date`, `datetime`, `dense_rank`, `group_concat`, `json_extract`, `lag`, `lead`, `rank`, `round`, `row_number`, `strftime`, `sum`, etc.).
- **Redacted ingestion and export:** Drops sensitive column names on load and applies `SessionHandoffPolicy` cleaning.

### Direct Structured Tables via `finish(table_ref=...)`

When a query returns many rows, the model passes `table_ref="t1"` or `"q1"` to `finish` instead of transcribing rows into prose:

1. The server exports the redacted dataset via `TurnTables.export_table(ref)` (caps in the protocol reference).
2. The Shadow DOM panel renders `<DataTable>` with live filtering bound to the server evidence receipt (`ev_*`).

## 5. Browser Runtime: Snapshots, Settling, and Form Handover

### Bounded Page Observations

`src/web_operation_agent/web_runtime/browser_contract.json` caps each frame at `200` interactive elements. When a modal dialog or drawer is open, `PageController` prioritizes controls inside the active dialog before adding background elements.

### Noise-Filtered Settling (`pageSettle.ts`)

Single-page apps commonly run heartbeat requests, SSE streams, or no-op attribute updates. `PageSettleWatch` in `frontend/src/runtime/pageSettle.ts` bounds wait time with explicit thresholds:

- `DEFAULT_SETTLE_QUIET_MS = 80` ms: Considered settled after 80 ms without meaningful DOM changes.
- `DEFAULT_SETTLE_MAX_WAIT_MS = 2000` ms: Maximum wait per action.
- `DEFAULT_LONG_TAIL_REQUEST_MS = 1500` ms: Requests older than 1.5 s do not block observation.
- **Ignored noise:** No-op attribute mutations where `oldValue === currentValue`, internal Shadow DOM panel changes, `#playwright-highlight-container`, `/woa/` internal endpoints, and `/events` SSE streams.

When a click, select, or navigation settles, `src/web_operation_agent/execution/loop_guard.py` caches `settled_action_observation` keyed by `obs.epoch` and `page_fingerprint(obs)` so the executor avoids a redundant `observe` round trip while the channel stays fresh.

### Form Prefill Receipts and Undo

After form prefill, the browser returns an `ActionReceipt` distinguishing three field outcomes:

- `applied_fields` and `previous_fields`: Filled values and prior values powering one-click `undoFormFill(turnId)` in the panel.
- `rejected_fields` and `candidate_labels`: Unmatched enum fields with actual available labels.
- `restricted_fields`: Password, secret, or file upload inputs skipped and focused for manual entry.
