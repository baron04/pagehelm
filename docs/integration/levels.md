---
title: Progressive Integration (L0–L3)
description: Adopt PageHelm incrementally from Level 0 browser panel + Element Plus integration helpers to Level 3 Skill writes, convention updates, and native form handover.
---

> **TL;DR:** Integrate PageHelm in four incremental stages inside this repository: **L0** embeds `assistant.js` and reuses in-repo `element-plus/` helpers; **L1** wires `ApplicationDescriptor`, `ApplicationPorts`, and same-actor reads (`testapps/contacts`); **L2** adds Markdown domain playbooks and disambiguation pills; **L3** enables reviewed Skill writes, symmetric `ConventionWritePort` updates, and native form prefill/submit handover.

[简体中文](../zh-cn/integration/levels.md)

## Overview of stages

| Level | Backend changes needed | Capabilities unlocked | Repository reference |
| --- | --- | --- | --- |
| **L0** | `/bootstrap` auth exchange only | Shadow DOM panel, semantic page observation, safe navigation & UI helpers | `frontend/src/integrations/element-plus/` |
| **L1** | Identity, routes & read ports | Same-actor API queries, in-memory `TurnTables` SQL, `<DataTable>` rendering | `testapps/contacts/adapter.py` |
| **L2** | Domain conventions & rules | Module disambiguation playbooks, compiled `entity_rules`, clickable `need_input` pills | `examples/yudao/src/.../domain_playbooks/` |
| **L3** | Write journal, verifier & Skills | Typed Skill writes, flat `update_record` / `toggle_record_status`, native submit & undoable form prefill | `WriteSafetyPipeline`, `SubmitPort`, `YudaoHost` |

## L0: Browser Runtime & UI Component Layer

Host pages load one self-contained bundle served directly from `EmbeddedGateway` at `/v1/runtime/assistant.js`:

```html
<script
  type="module"
  src="/woa-agent/v1/runtime/assistant.js"
  data-woa-bootstrap="/woa-agent-boot/bootstrap"
></script>
```

The bootstrap endpoint exchanges the host's authenticated session for a short-lived capability via `EmbeddedGateway.bootstrap_config(...)`, keeping tokens out of local bundles and browser storage leaks. Your origin must also serve a pinned same-origin `PageController` module exporting `PageController` and `getElementByIndex`.

### Reusing in-repo Element Plus integration helpers

Apps built on Vue 3 + Element Plus do not need custom DOM scrapers. `frontend/src/integrations/element-plus/` extracts shared framework handlers away from core runtime code (`frontend/src/runtime/` remains zero `.el-*`):

- `table.js`, `select.js`, `tree-select` support, `date-picker.js`, `form.js`, and `vue.js` handle mounted popovers, option labels, table row identities, and drawer push offsets (`--woa-drawer-offset`);
- `frontend/scripts/build-yudao.mjs` compiles the Element Plus layer together with app-specific route facts into served JS assets (`frontend/dist/integrations/yudao/`).

Applications using another UI library implement their own sibling directory under `frontend/src/integrations/<app>/` exporting `readElementPageControl`, optional `openPage(url)`, and `readPageBlocker()`.

## L1: Minimal Backend Adapter (`testapps/contacts`)

Study `testapps/contacts/adapter.py` as a complete runnable blueprint structurally distinct from `yudao`:

1. **Declare versioned metadata (`ApplicationDescriptor`):** Specify `application_id`, exact `public_origin`, `navigation_mode` (`"document"` or `"spa"`), same-origin `page_controller_module`, and `business_tools.catalog_sha256` so stale bundles fail fast at startup.
2. **Bind `ApplicationPorts` via `bind_application(adapter)`:**
   - `identity` (`ApplicationIdentityPort`): resolves trusted server identity (`PrincipalContext` with `tenant_id`, `actor_id`, `scopes`) from same-origin credentials—never trusting DOM text or browser-supplied user IDs.
   - `routes` (`DeclaredRoutePort`): validates navigable URLs and declares `session_handoff_query_keys` for outbound redaction.
   - `reads` / `data_classifier`: registers typed read operations on `BusinessToolRuntime`, annotates `QueryEvidence` (`matched_count`, `count_exact`, `looked_for`, `exact_matches`), and optionally feeds rows into `TurnTables` (`sql` + `<DataTable>` attachment on `finish`).
3. **Verify contracts before touching real models:** Run the core black-box suites against your test app (`scripts/check.py` Contacts lane runs both offline contracts and headless Chromium integration).

## L2: Domain Playbooks, Conventions & Disambiguation

To prevent terminology mix-ups without writing fragile per-page scripts:

- **Domain playbooks (`domain_playbooks/*.md`):** Supply organization-reviewed Markdown definitions for module-scoped terms (for example, "customer" meaning CRM Customer vs ERP Counterparty depending on active module) and organizational calendar semantics (`date_range`), accessed lazily via `load_domain_guide`.
- **Compiled business rules (`entity_rules`):** Expose verified backend delete/status transition guards extracted at build/deploy time from source allowlists, cited cleanly when explaining why a record cannot be deleted.
- **Interactive disambiguation pills (`UX-02`):** When `find_record` or read lookups return 2–4 real same-actor candidates sharing a name, `finish(status="needs_input", input_options=[...])` renders clickable choice pills inside the panel while keeping strict sanitization on payloads.

## L3: Verified Writes, Convention Updates & Handover

Writes remain disabled (`read-only`) until explicitly opted in. Once enabled, every mutation routes through the shared `WriteSafetyPipeline` (`BusinessWriteService`):

1. **Reviewed typed Skill writes (`SKILL.md` + `agents/woa.toml`):** Author declarative packages scoped to exact entities/pages and verified `ToolDefinition.effect_contract` projections; register `ResultPostconditionVerifier` and `MutationReconciliation`.
2. **Symmetric flat convention updates (`ConventionWritePort`):** Opt into `"updates"` capability to expose generic `update_record` (only where create/update and detail GET expose 1:1 symmetric flat writable fields without nested line arrays or password/status fields) and `toggle_record_status` (strictly reversible two-state transitions), verified via shared contract tests across both `testapps/contacts` and `yudao`.
3. **Native form submit & undoable prefill (`SubmitPort`, `UX-03`, `UX-05`):** Declare `SubmitConventions` (`volatile_keys`, `record_key`, `line_key`, `name_key`, `epoch_zone`, `decode_write`) for approved native form submissions, or offer `"Prefill form only"` / partial restricted-control handover (`password` / file inputs are never autofilled, auto-scrolled/focused for human input, and prior autofilled scalar values can be reverted with one click via `undoFormFill`).
