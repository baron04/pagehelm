---
title: Tools, Capabilities & Checks
description: Catalog of model-visible tools, GoalPort capability declarations, and ProgramCheckSpec verification checks.
---

The tools visible to the model on each step are projected dynamically by the server from the current session state and permissions (`ToolSurface` in `execution/tools.py`). Adapters declare supported optional capabilities via `GoalPort.capabilities()` (and optional convention write-port hooks via `ConventionWritePort.capabilities()`), and every turn runs code-level `ProgramCheckSpec` verification before delivery.

## 1. Model-Visible Tool Catalog

### Browser Action and Page Tools (`TOOL_SCHEMAS` / `ToolSurface`)

| Tool | Core arguments | Purpose & boundary |
| --- | --- | --- |
| `observe` | `{}` | Captures a bounded DOM snapshot (`<= 200` elements); read-only |
| `click` | `ref`, optional `expect_navigation` | Targets only handles from the latest `epoch`; omit `expect_navigation` unless JS triggers navigation |
| `fill` | `ref`, `value` | Enters text into a control; password and restricted inputs are skipped for human entry |
| `select` | `ref`, `value`, optional `option_label` | Selects an option in native `<select>` or single-select dropdowns by visible label |
| `submit` | `ref`, optional `expect_navigation` | Submits a native form (subject to safety mode and approval gates) |
| `goto` | `url` | Allows only exact links from the current page or actor-verified `ui_path` values |
| `back` | `{}` | Navigates one step back in browser history |
| `scroll` | `direction` (`up`\|`down`), optional `pages` | Scrolls viewport within `MAX_SCROLL_PAGES` and returns a fresh observation |
| `wait_for` | optional `ms` (`0..10000`) | Bounded wait for async rendering followed by observation |
| `highlight` | `ref` (`^e[0-9]+$`) | Highlights a control and ends the turn for the operator to click manually |
| `set_target` | `path`, `selected`, `filters`, `query`, `groups`, `filters_absent` | Declares expected target page state prior to actions for server verification |
| `apply_facet` | `ref`, `active` | Toggles declared page filters or group controls (requires `allow_page_facets`) |
| `enter_application` | `ref` | Follows an application entry link while stripping session handoff query keys |
| `prefill_form` | `{}` | Applies a server-issued transient draft to the active form without submitting |
| `finish` | `done`, `why` or `outcome`, `answer` (plus `table_ref`, `status="need_input"`, `options`) | Closes the turn with verified evidence, `<DataTable>` reference, or disambiguation pills |

Internal actions (`handoff_form`, `reveal`, `form_model`, `form_submit`) are dispatched only by server safety pipelines and are not exposed as free-form model tools.

### Same-Actor Data Read and Computation Tools

- **`inspect_data(query, entity?)`:** Searches entities, field labels, and read-only page scopes visible to the active actor (metadata only, not row facts).
- **`resolve_data(deliverables[])`:** Runs 1–4 deterministic `fact`, `list`, or `aggregate` (`count`, `sum`, `min`, `max`) deliverables in one call.
- **`compute(operation, dataset_ref, ...)`:** Executes `Decimal` operations (`filter`, `count`, `sum`, `min`, `max`, `argmin`, `argmax`) over server-held evidence datasets.
- **`sql` and `date_range` (`TurnTables`):** Runs a single read-only `SELECT` or `WITH` query over per-turn in-memory SQLite tables and delivers rows via `finish(table_ref=...)`.

### Clarification, Capability Discovery, Skills, and Submit Tools

| Tool | Arguments | Purpose |
| --- | --- | --- |
| `ask_user` | `question`, `response_kind`, `choice_ids` | Pauses for missing required input or disambiguation; user reply is task input, not write approval |
| `withdraw_request` | `{}` | Cleanly withdraws a pending clarification when the operator cancels |
| `check_capability` | `{}` | Inspects installed write capabilities before reporting a capability gap on mutation requests |
| `load_skill` | `skill_id` | Loads a packaged `SKILL.md` procedure on demand; grants no tools or permissions |
| `enter_skill_app` / `open_skill_route` | `skill_id`, `bindings?` | Opens a server-verified read-only module or detail route declared by a Skill |
| `request_submit` | `index` | Invokes `WritePipeline.run_submit_pipeline`: reads the form, checks provenance, displays approval card, clicks once after approval, and reads back |

### Optional `SourceTree` Retrieval Tools

Enabled only when the host injects an allowlisted `SourceTree` (`runtime_source=True`):

- `lookup_source(query)`: Lexical search over allowlisted files (returns bounded `source#N` excerpts).
- `lookup_source_history(kind, ...)`: Reads bounded local Git metadata (`owners`, `blame`, `commits`, `changes`), returning `history#N`.
- `read_source(path, start_line, end_line)`: Line-range read of an allowlisted source file.
- `open_source_route(url)` / `open_source_candidate(candidate_id, bindings)`: Opens only read-only view routes confirmed in allowlisted source.

## 2. `GoalPort` Capability Declarations

`src/web_operation_agent/goal/ports.py` separates mandatory baseline capabilities from optional capabilities declared via `capabilities()`. When an optional capability is omitted, the core explains the limit to the user; adapters must never fake support.

- **Required (`REQUIRED`):** `index`, `records`, `facts`, `aggregates`, `pages`.
- **Optional (`OPTIONAL`):**

| Key | Behavior enabled |
| --- | --- |
| `facets` | Page filter and group toggles (`apply_facet`, `set_target` filter checks) |
| `paging` | Deterministic list pagination and full-coverage reporting |
| `sections` | Sub-tab or section switching on detail/list pages |
| `lines` | Reading embedded child line tables on master-detail records |
| `handoffs` | Stripping sensitive handoff query parameters on in-app redirects |
| `fill` | Structured pre-handover form prefill with `undoFormFill` |
| `search` | Binding native page search inputs and verifying applied search criteria |
| `source` | Compiled source rules and read-only route candidates |
| `values` | Looking up valid same-actor candidate values before write proposals |
| `updates` | Symmetric flat `ConventionWritePort` (`update_record`, `toggle_record_status`) |

### `ConventionWritePort` Optional Capabilities (`WRITE_PORT_OPTIONAL`)

- `companion_readback`: Enables same-actor companion write readback (`verify_companion_readback`) and pre-dispatch destructive-delete child row count inspection (`count_companion_rows`).
- `preserved_field_extraction`: Enables adapter-specific preserved field extraction (`extract_preserved_field`) when reading full flat/projected records before update.
- `reference_variants`: Enables adapter-specific display/token variants (`reference_provenance_variants`) during foreign-key provenance validation.

When an optional capability is omitted from `port.capabilities()`, core ignores any hook on the port, marks companion writes as unverified, and fails closed on destructive-delete companion updates.

## 3. Programmatic Turn Verification (`ProgramCheckSpec`)

Before delivering final answers or advancing side effects, the runtime executes code-level checks over `TurnTrace` and evidence receipts:

### Core Checks (`CORE_CHECK_SPECS`, 7 checks)

| Check | Purpose |
| --- | --- |
| `untraced_numbers` | Blocks numbers in the answer not grounded in same-turn reads, computations, or `<DataTable>` |
| `model_delivery_rejected` | Rejects malformed `finish` payloads and triggers bounded repair |
| `completion_review` | Confirms all sub-parts of multi-part requests are finished or explicitly flagged |
| `slim_verify` | Final consistency check between outgoing answer text and attached evidence |
| `failed_read_repeated` | Stops identical failing read arguments from repeating in the same turn |
| `same_step_stalled` | Detects loops where the page fingerprint does not change |
| `model_answer_not_delivered` | Ensures completed turns always emit a displayable delivery |

### Adapter Extension Checks (15-check reference example)

- **Read/write & draft discipline:** `read_only_question`, `draft_only`, `dangerous_unconfirmed`, `unrequested_draft_field`, `draft_handoff_required`
- **Delivery completeness & UI consistency:** `delivery_sensitive`, `delivery_missing_receipt`, `delivery_foreign_receipt`, `delivery_page_handoff`, `delivery_page_home`, `delivery_dialog_missing`, `delivery_page_masked`, `delivery_write_unverified`, `delivery_page_obligation`, `delivery_incomplete`
