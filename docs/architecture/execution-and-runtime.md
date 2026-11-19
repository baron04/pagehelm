---
title: Execution Engine & Browser Runtime
description: Inside ModelDrivenTurnExecutor, TurnBudget, concurrent read commit ordering, Decimal summaries, TurnTables SQL, and bounded DOM settling.
---

> **TL;DR:** Every user request executes inside a single shared-budget loop (`ModelDrivenTurnExecutor` + `TurnHarness` + `TurnBudget`), batching pure reads concurrently while committing tables and receipts in deterministic order, computing exact Decimal summaries (`read_summary`) or ephemeral SQLite analytics (`TurnTables`), and driving the tab cleanly with bounded DOM observation (`<= 200` elements) and noise-suppressed settling.

[简体中文](../zh-cn/architecture/execution-and-runtime.md)

## 1. Turn orchestration and shared budget

```text
User Prompt
   |
   v
TurnHarness (session binding, action fence, approval watch, trace)
   |
   +--> TurnBudget(steps, seconds)  <-- Single shared counter + monotonic clock
   |       - Every model call (including retries) consumes 1 step
   |       - Moving from API read -> SQL -> UI navigation NEVER resets budget
   |
   v
ModelDrivenTurnExecutor loop
   |---> Concurrent pure reads  ---> Deterministic main-thread commit (t1, ev_*)
   |---> Bounded browser action ---> PageSettleWatch (80ms quiet / 2s max)
   |---> finish(table_ref=...)  ---> Redacted <DataTable> in Shadow DOM panel
   |---> finish(status="need_input", options=[...]) ---> One-click option pills
```

Unlike multi-agent pipelines where sub-agents silently spawn independent timeouts, `TurnBudget` (`src/web_operation_agent/execution/budget.py`) enforces one atomic step counter and one wall-clock deadline across the entire turn:

- `TurnBudget.reserve()` checks cancellation and remaining wall time before bumping `used` up to `steps`.
- Every bounded read checkpoint (`stopped_read` in `data_tools.py`) inspects the same `TurnBudget`, producing explicit terminal codes: `"user_cancelled"`, `"step_budget"`, `"token_budget"`, or `"wall_clock_budget"`.

---

## 2. Concurrent read batching with sequential commit

When the model issues multiple read-only calls in a single assistant step (or an adapter runs multi-slice pagination through a procedure such as `_ConcurrentReadProcedure`), workers perform network fetches as **pure reads**:

- Worker threads redact payloads and validate row bounds without mutating shared turn state (`TurnTables`, knowledge caches, or receipt counters).
- Once the batch resolves, the main turn thread commits each slice in deterministic issue order so table handles (`t1`, `t2`, ...) and evidence receipts (`ev_<hex12>_<n>`) remain strictly reproducible across runs and replay traces.

---

## 3. Exact server-side aggregations (`read_summary.py`)

Counting or summing across hundreds of records should never rely on floating-point guesses in LLM prose or slow DOM loops. `src/web_operation_agent/execution/read_summary.py` executes deterministic, Decimal-backed aggregations directly against an actor-bound read:

| Constraint / Capability | Verified Behavior |
| --- | --- |
| Row & response bounds | `MAX_ROWS = 5000`, `MAX_RESULT_CHARS = 100_000` |
| Supported column kinds | `text`, `number`, `boolean`, `date`, `datetime`, `relation` |
| Filter operators | `=`, `!=`, `contains`, `prefix`, `in`, `>`, `>=`, `<`, `<=`, `between`, `empty`, `not_empty` |
| Exact measure set | `_MEASURES = ("count", "count_distinct", "sum", "min", "max")` (Decimal math; **no `avg`** here—use `TurnTables` SQL when computing averages or joins) |
| Unit safety | `SummaryColumn.unverified_unit=True` marks monetary numbers whose unit has not been convention-proven so raw figures are shown honestly without guessing currency |
| Truncation safety | When an upstream response marks data `truncated=True`, core answer checks forbid presenting a partial page slice as an exhaustive dataset total |

---

## 4. Ephemeral turn SQL (`TurnTables`) and `<DataTable>`

For multi-endpoint joins, group-by rankings, average calculations, or follow-up slicing over already observed API responses, adapters can bind an ephemeral in-memory SQLite workspace per turn (`TurnTables`):

- **Isolation & cleanup:** Uses `sqlite3.connect(":memory:", cached_statements=0)` with `SQLITE_LIMIT_ATTACHED = 0`, destroyed at turn end via `TurnTables.close()`.
- **Hard capacity caps:**
  - `MAX_TABLES = 36` tables per turn (`t1`, `t2`, ...; saved query results `q1`, `q2`, ...)
  - `MAX_ROWS = 5000` rows per table
  - `MAX_COLUMNS = 128` columns per table
  - `MAX_SQL_ROWS = 200` / `MAX_SQL_CHARS = 14000` returned to model context, with a `1.5s` query deadline
- **Strict SQL safety:** Accepts a single `SELECT` or `WITH` query only (`sql_refused` otherwise); strips SQL comments while preserving string literals; permits only an explicit safe scalar/window/aggregate function allowlist (`abs`, `avg`, `coalesce`, `count`, `date`, `datetime`, `dense_rank`, `group_concat`, `json_extract`, `lag`, `lead`, `rank`, `round`, `row_number`, `strftime`, `sum`, etc.).
- **Automatic redaction:** Filters out secret-named columns and applies `SessionHandoffPolicy` before inserting rows and again before exporting cells.

### Zero-copy structured reporting with `finish(table_ref=...)`

Rather than asking the model to transcribe 150 rows into Markdown prose—introducing transcription mistakes and wasting tokens—the model passes `table_ref="t1"` or `table_ref="q1"` to `finish`:

1. Server resolves the reference via `TurnTables.export_table(ref)` (capped at `EXPORT_MAX_COLS = 24`, `EXPORT_MAX_ROWS = 2000`, `EXPORT_CELL_CHARS = 300`, `EXPORT_TITLE_CHARS = 120`).
2. `export_table` enforces credential/handoff redaction across every exported header, cell, and title.
3. The panel renders a native interactive `<DataTable>` with instant client-side filtering and full row visibility, backed by a real turn evidence receipt (`ev_*`).

---

## 5. Browser runtime: DOM snapshots, settling, and UI helpers

### Bounded Page Observation (`MAX_OBSERVATION_ELEMENTS = 200`)

`src/web_operation_agent/web_runtime/browser_contract.json` fixes `observation.max_elements = 200`. When a busy admin screen exposes an active modal dialog or drawer, `PageController` prioritizes interactive controls inside the active modal surface first before surrounding background elements, tagging controls cleanly for targeting, candidate lookup, and form fill.

### Noise-suppressed settling (`pageSettle.ts`)

Modern SPAs continuously fire background pings, SSE streams, and CSS attribute churn. `frontend/src/runtime/pageSettle.ts` implements `PageSettleWatch` with tight deterministic bounds:

- `DEFAULT_SETTLE_QUIET_MS = 80` ms of DOM quiet before declaring settled
- `DEFAULT_SETTLE_MAX_WAIT_MS = 2000` ms maximum wait per action
- `DEFAULT_LONG_TAIL_REQUEST_MS = 1500` ms threshold beyond which pre-existing long-tail network requests are ignored
- **Ignored noise:** no-op attribute writes (`oldValue === currentValue`), mutations inside the Shadow DOM assistant panel, `#playwright-highlight-container`, internal `/woa/` endpoints, and `/events` SSE streams.

Right after a navigation, click, or select settles cleanly, `src/web_operation_agent/execution/loop_guard.py` records `settled_action_observation` keyed by `obs.epoch` and `page_fingerprint(obs)`. If the channel still reflects that exact state, the executor reuses the settled observation immediately instead of burning an extra model round-trip calling `observe`.

### Framework integration modules & safe form handoff

Generic runtime code stays library-agnostic. Reusable Vue / Element Plus DOM helpers live cleanly under `frontend/src/integrations/element-plus/`:

- `date-picker.js` (`isElDateRangeInput`, date panel helpers)
- `form.js` (label extraction, form bounds `depth: 8`, `nodes: 6000`, `bytes: 200000`)
- `select.js` (`readElSelectVisibleCandidates`, inner-input avoidance)
- `table.js` (table header/row readers)
- `vue.js` (framework state inspection)

When prefilling forms in `assisted-draft` or handing off an operation, `ActionReceipt` reports structured per-field outcomes back to the turn loop:

- `applied_fields` and `previous_fields` (enabling one-click host rollback via `undoFormFill(turnId)`)
- `rejected_fields` and `candidate_labels` (when a dropdown or autocomplete option does not match)
- `restricted_fields` (secret/password controls intentionally skipped by prefill and focused for manual entry)

For ambiguous requests (`"Update Zhang's record"` when two records exist), the model terminates cleanly via `finish(status="need_input", options=[...])`, rendering clickable clarifying pills in the Shadow DOM composer rather than guessing.
