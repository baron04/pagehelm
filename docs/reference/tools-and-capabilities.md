---
title: Built-in Tools & Capabilities
description: Complete catalog of model-visible browser tools, read/SQL/compute primitives, Skill/submit tools, GoalPort capability declarations, and ProgramCheckSpec enforcement.
---

> **TL;DR:** Tool availability in PageHelm is controlled dynamically by server session state (`execution/tools.py` `ToolSurface`), never prompt promises. Adapters declare explicit read/write capabilities via `GoalPort.capabilities()`, and every completed turn passes deterministic `ProgramCheckSpec` checks before reaching the operator.

[简体中文](../zh-cn/reference/tools-and-capabilities.md)

## 1. Model-visible tool catalog

### Browser primitives & page state (`TOOL_SCHEMAS` / `ToolSurface`)

| Tool | Arguments | Purpose & safety boundary |
| --- | --- | --- |
| `observe` | `{}` | Captures a fresh bounded DOM snapshot (`<= 200` elements) without mutating page state |
| `click` | `ref`, optional `expect_navigation` | Clicks element reference from latest epoch only (`expect_navigation=true` only when routing via JS) |
| `fill` | `ref`, `value` | Types text into a form control; secret/password controls are excluded or delegated |
| `select` | `ref`, `value`, optional `option_label` | Chooses native `<select>` option or combobox item by visible label |
| `submit` | `ref`, optional `expect_navigation` | Submits the containing form (subject to mode & approval checks) |
| `goto` | `url` | Navigates only to an exact page link or adapter-authorized `ui_path` |
| `back` | `{}` | Navigates back to the previous history entry |
| `scroll` | `direction` (`up`\|`down`), optional `pages` | Scrolls vertically up to `MAX_SCROLL_PAGES` viewports and refreshes observation |
| `wait_for` | optional `ms` (`0..10000`) | Bounded page load wait followed by fresh observation |
| `highlight` | `ref` (`^e[0-9]+$`) | Highlights an interactive control for the operator and ends the turn without clicking |
| `set_target` | `path`, `selected`, `filters`, `query`, `groups`, `filters_absent` | Declares target page state before acting; server checks settled arrival |
| `apply_facet` | `ref`, `active` | Toggles one declared filter or group facet (`allow_page_facets`) |
| `enter_application` | `ref` | Follows an observed Main application link while scrubbing handoff secrets |
| `prefill_form` | `{}` | Applies server-issued transient form draft; never submits |
| `finish` | `done`, `why` / or `outcome`, `answer` (or `table_ref`, `status="need_input"`, `options`) | Ends turn cleanly with verified evidence, `<DataTable>` reference, or clarification pills |

*(Internal browser actions `handoff_form`, `reveal`, `form_model`, and `form_submit` are issued strictly by the server runtime and are never exposed as free model parameters.)*

---

### Data reads, Decimal `compute`, & ephemeral SQL (`TurnTables`)

- **`inspect_data(query, entity?)`:** Discovers the active actor's indexed entity keys, field labels, and readonly page scopes (metadata only—not row proof).
- **`resolve_data(deliverables[])`:** Runs deterministic actor-scoped queries (`fact`, `list`, `aggregate` with `op` in `count`, `sum`, `min`, `max` plus filters/sort/limit up to 4 deliverables per step).
- **`compute(operation, dataset_ref, field?, comparison?, threshold?, choices?)`:** Executes Decimal calculations (`filter`, `count`, `sum`, `min`, `max`, `argmin`, `argmax`) over current server-retained evidence (`answer/answer_protocol.py`).
- **Ephemeral `TurnTables` (`turn_tables.py` pattern):** Adapters supporting table analytics ingest actor-bound API slices into `:memory:` SQLite tables (`t1`, `t2`, ...; saved results `q1`, `q2`, ...) queried with single read-only `SELECT` / `WITH` statements (`sql`) and helper `date_range`, then deliver zero-copy `<DataTable>` views via `finish(table_ref=...)`.

---

### Clarification, capability, Skill & native submit tools

| Tool | Arguments | Purpose |
| --- | --- | --- |
| `ask_user` | `question`, `response_kind` (`free_text`\|`single_choice`), `choice_ids` | Pauses turn for missing parameters or candidate choice; never acts as write confirmation |
| `withdraw_request` | `{}` | Cleanly cancels a pending clarification when the operator backs out |
| `check_capability` | `{}` | Searches installed write capabilities before concluding `capability_gap` |
| `load_skill` | `skill_id` | Loads optional `SKILL.md` domain instructions; grants zero write authority on its own |
| `enter_skill_app` / `open_skill_route` | `skill_id`, `bindings?` | Enters or opens read-only Skill-associated destinations validated by the server |
| `request_submit` | `index` | Initiates `WritePipeline` `run_submit_pipeline`: reads full form, checks provenance, displays approval card, presses once after grant, reads back under same actor |

---

### Opt-in `SourceTree` lookup tools

Available only when the deployment injects an explicit `SourceTree` allowlist (`runtime_source=True`):

- `lookup_source(query)` — lexical search across allowlisted repository files (returns bounded `source#N` excerpts).
- `lookup_source_history(kind, path?, start_line?, end_line?, target_commit?)` — bounded local Git metadata (`CODEOWNERS` / `owners`, `blame`, `commits`, `changes`) returning `history#N`.
- `read_source(path, start_line, end_line)` — reads bounded lines from an allowlisted path.
- `open_source_route(url)` / `open_source_candidate(candidate_id, bindings)` — opens view-only routes proven by allowlisted source evidence.

---

## 2. `GoalPort` capability declaration contract

[`src/web_operation_agent/goal/ports.py`](../architecture/overview.md) distinguishes mandatory read fundamentals from optional capabilities declared explicitly via `capabilities()` (`GoalPort` — adapters never fake support for undeclared capabilities):

### Required core capabilities (`REQUIRED`)

`index` · `records` · `facts` · `aggregates` · `pages`

### Optional declared capabilities (`OPTIONAL`)

| Capability | What it unlocks when declared by the adapter |
| --- | --- |
| `facets` | Page filter and group toggles (`apply_facet`, `set_target` facet checks) |
| `paging` | Deterministic multi-page navigation & viewport coverage tracking |
| `sections` | Named tabbed/section views on complex detail/list pages |
| `lines` | Embedded document line-item tables inside master-detail forms |
| `handoffs` | Session handoff URL parameter scrubbing & safe inter-app transitions |
| `fill` | Structured form prefill before user takeover (`undoFormFill`) |
| `search` | Native page search bar binding and applied read digests |
| `source` | Allowlisted source rules and view-only route evidence |
| `values` | Validated same-actor candidate value lookup before write declaration |
| `updates` | Symmetric flat 1:1 `ConventionWritePort` (`update_record` & `toggle_record_status`) |

---

## 3. Programmatic turn verification (`ProgramCheckSpec`)

Before delivering final responses or committing side effects, programmatic checks inspect `TurnTrace` and evidence bindings to fail closed on fabricated numbers, unverified claims, or safety violations:

### Core checks (`CORE_CHECK_SPECS`, 7 specs)

| Check ID | Enforced invariant |
| --- | --- |
| `untraced_numbers` | Rejects quantitative claims in final prose unsupported by server read/compute/table receipts |
| `model_delivery_rejected` | Catches malformed or schema-violating terminal answer structures for bounded repair |
| `completion_review` | Verifies requested multi-clause user goals are addressed or marked explicitly incomplete |
| `slim_verify` | Runs final evidence consistency verification before surfacing response text |
| `failed_read_repeated` | Stops infinite retry loops when the same failing read repeats without parameter change |
| `same_step_stalled` | Detects non-advancing step loops on unchanged page/state fingerprints |
| `model_answer_not_delivered` | Ensures turns terminating normally produce an operator-visible delivery payload |

### Adapter-level policy checks (reference adapter: 15 `ProgramCheckSpec` rules)

- **Scope & draft discipline:** `read_only_question`, `draft_only`, `dangerous_unconfirmed`, `unrequested_draft_field`, `draft_handoff_required`
- **Delivery, receipt & screen integrity:** `delivery_sensitive`, `delivery_missing_receipt`, `delivery_foreign_receipt`, `delivery_page_handoff`, `delivery_page_home`, `delivery_dialog_missing`, `delivery_page_masked`, `delivery_write_unverified`, `delivery_page_obligation`, `delivery_incomplete`
