---
title: L0–L3 Progressive Integration
description: Adopt PageHelm step by step from L0 embedded panel and Element Plus helpers to L1 same-actor reads, L2 domain playbooks, and L3 verified writes.
---

> **TL;DR:** Adopt PageHelm incrementally: **L0** embeds `assistant.js` and reuses `frontend/src/integrations/element-plus/`; **L1** wires `ApplicationDescriptor`, `ApplicationPorts`, and typed reads (`testapps/contacts/adapter.py`); **L2** adds Markdown domain playbooks, compiled rules, and `need_input` pills; **L3** enables `WritePipeline`, symmetric `ConventionWritePort` updates, reviewed write Skills, and reversible form handoff (`SubmitPort`).

[简体中文](../zh-cn/integration/levels.md)

## 1. Progressive integration ladder

```text
  L0: Embedded Panel & DOM Helpers     -> /v1/runtime/assistant.js + /bootstrap + PageController
         |
         v
  L1: Same-Actor Identity & Reads      -> ApplicationDescriptor + ApplicationPorts + QueryEvidence
         |                                (TurnTables SQL + <DataTable> + read_summary)
         v
  L2: Domain Disambiguation & Rules    -> domain_playbooks/*.md + compiled entity_rules + need_input pills
         |
         v
  L3: Verified Writes & Form Handoff   -> WritePipeline + SKILL.md write ops + ConventionWritePort
                                          + SubmitPort / MutationPrefillOffer + undoFormFill
```

| Level | Required integration work | Capabilities unlocked | Repository reference |
| --- | --- | --- | --- |
| **L0** | Mount `assistant.js` + `/bootstrap` endpoint | Shadow DOM panel, DOM snapshots (`<= 200`), Element Plus helpers | `frontend/src/integrations/element-plus/` |
| **L1** | `ApplicationDescriptor`, Identity, Routes & read ports | Same-actor queries, `TurnTables` SQL, `<DataTable>` delivery | `testapps/contacts/adapter.py` |
| **L2** | `domain_playbooks/*.md`, `entity_rules`, disambiguation | Terminology resolution, prerequisite explanations, `need_input` pills | `examples/yudao/` domain playbooks |
| **L3** | Write journal, verifier, Skills / `ConventionWritePort` / `SubmitPort` | Confirmed typed Skill writes, flat `update_record`, native submit + undo prefill | `src/web_operation_agent/business/write_pipeline.py` |

---

## 2. L0: Browser Runtime & UI Component Layer

Host applications mount one version-pinned bundle served by `EmbeddedGateway` at `/v1/runtime/assistant.js`:

```html
<script
  type="module"
  src="/woa-agent/v1/runtime/assistant.js"
  data-woa-bootstrap="/woa-agent-boot/bootstrap"
></script>
```

On load, the runtime requests `data-woa-bootstrap`, where the backend validates the operator's live application session and calls `EmbeddedGateway.bootstrap_config(...)` to mint a short-lived capability—never shipping model API keys or static tokens inside HTML. Your origin also hosts a pinned same-origin `PageController` module exporting `PageController` and `getElementByIndex`.

### Reusing `frontend/src/integrations/element-plus/`

For Vue 3 + Element Plus applications, PageHelm separates all UI library DOM knowledge cleanly outside `frontend/src/runtime/` into five shared helpers under `frontend/src/integrations/element-plus/`:

- `table.js` — extracts tabular columns, visible rows, and row identity bindings.
- `select.js` — reads `body`-mounted dropdown candidates (`readElSelectVisibleCandidates`) without triggering inner-input loops (`shouldAvoidSelectInnerInput`).
- `date-picker.js` — recognizes range/date pickers (`isElDateRangeInput`) and emits native updates cleanly.
- `form.js` — extracts field labels and drawer offsets (`--woa-drawer-offset`) within `depth: 8`, `nodes: 6000`, `bytes: 200000`.
- `vue.js` — binds component reactivity state cleanly when filling managed controls.

Applications built with another frontend framework mirror that structure under `frontend/src/integrations/<app>/` exporting `readElementPageControl`, optional `openPage(url)`, and `readPageBlocker()`.

---

## 3. L1: Minimal Read Adapter (`testapps/contacts/adapter.py`)

`testapps/contacts/adapter.py` demonstrates a complete runnable adapter structurally distinct from `yudao`:

1. **Declare `ApplicationDescriptor`:** Pin `application_id`, `public_origin`, `navigation_mode` (`"document"` or `"spa"`), `page_controller_module`, and `business_tools.catalog_sha256` so stale client assets fail cleanly rather than operating mismatched APIs.
2. **Compose `ApplicationPorts` via `bind_application(adapter)`:**
   - `IdentityPort`: maps authenticated host headers/cookies into `PrincipalContext(tenant_id, actor_id, scopes)` — never trusting user IDs from DOM text or request JSON.
   - `ApplicationRoutePort`: validates internal routes and lists `session_handoff_query_keys` scrubbed before observations enter logs or models.
   - `EntityReadTools` / `ReferenceLookupTools` / `PageEntryTools` / `GoalCandidateTools`: register read-only queries inside `BusinessToolRuntime`, emitting structured `QueryEvidence` (`matched_count`, `count_exact`, `looked_for`, `exact_matches`) and feeding `TurnTables` SQL or `<DataTable>` responses (`bind_matching_identity`).
   - `GoalPort.capabilities()`: explicitly declare optional capabilities (`facets`, `paging`, `sections`, `lines`, `handoffs`, `fill`, `search`, `source`, `values`, `updates`)—never fake unsupported capabilities.

---

## 4. L2: Domain Playbooks, Rules & Disambiguation Pills

Eliminate enterprise terminology confusion without writing per-screen workflow scripts:

- **Domain playbooks (`domain_playbooks/*.md`):** Concise Markdown explaining domain distinctions (e.g., how CRM leads convert to customers vs ERP partners, fiscal period definitions), loaded lazily by `load_domain_guide`.
- **Compiled rules (`entity_rules`):** Precondition summaries extracted via `SourceTree` allowlists from reviewed backend validators so the assistant accurately explains why a delete or status transition is blocked.
- **Disambiguation pills (`finish(status="need_input", options=[...])`):** When a name search hits 2–4 matching records visible to the same actor, render verified selectable `input_options` chips instead of guessing which record the operator intended.

---

## 5. L3: Verified Writes, Convention Updates & Form Handover

Only enable writes once L1 reads and actor isolation pass contract checks (`mode = "verified-operate"`). All state-changing paths route through `WritePipeline`:

1. **Reviewed Skill Operations (`SKILL.md` + `agents/woa.toml`):** See [Skills & Typed Writes](./skills-and-writes.md) for package structure, `prepare_mutation` / `execute_mutation`, `ResultPostconditionVerifier`, and `MutationReconciliation`.
2. **Symmetric Convention Updates (`ConventionWritePort`):** Declaring the `"updates"` capability derives flat `update_record` specs for entities with 1:1 update/get scalar field symmetry (auto-excluding secret fields and nested line arrays) and two-state reversible `toggle_record_status` switches.
3. **Native Form Submission & Undoable Prefill (`SubmitPort`):** Declaring `SubmitConventions` lets `request_submit` show the full diff card (`target`, field transitions, button), obtain one-time approval, press once, and verify by readback—or fall back to reversible form prefill with `undoFormFill(turnId)` and focus transfer on secret/upload fields.

### Adapter verification checklist

Before shipping an adapter change, verify all contract layers without hitting paid LLMs:

- Run core neutral boundary checks (`scripts/check_core_boundary.py`)
- Run adapter contract tests (`testapps/contacts` or your adapter test module under `scripts/check.py`)
- Exercise end-to-end browser flows (`L0` panel mount, `L1` table filter, `L2` `need_input`, `L3` approval + readback) against synthetic resettable test data.
