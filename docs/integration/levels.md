---
title: L0–L3 Integration Guide
description: "Incrementally integrate PageHelm into a first-party web application: prerequisites, L0 panel embedding, L1 same-actor reads, L2 playbooks and disambiguation, and L3 verified writes."
---

Applications can adopt PageHelm incrementally from L0 to L3 without rewriting existing backend APIs up front.

> Coding-agent onboarding workflow: load `.agents/skills/integrate-pagehelm/SKILL.md` (`$integrate-pagehelm`) to inspect the host application read-only, scaffold L0–L1 integration first, and enforce identity/write safety checks before adding L2 or L3.

## Prerequisites

Before integrating, confirm that the host application meets three requirements:

- **Same-origin deployment:** The PageHelm gateway (default prefix `/woa-agent`) is served on the same browser origin as the host application.
- **Server-side session resolution:** The host backend can resolve the signed-in actor from the incoming request cookies or headers during `/bootstrap`.
- **Single-script frontend mount:** The host page can load the same-origin `assistant.js` and `PageController` ES modules. Vue 3 + Element Plus apps can reuse the bundled helpers; other UI frameworks implement the same helper interface.

## 1. L0–L3 Overview

| Level | Host integration work | Capabilities enabled | Repository reference |
| --- | --- | --- | --- |
| **L0** | Mount `/v1/runtime/assistant.js` and `/bootstrap` | Shadow DOM panel, bounded DOM snapshots (`<= 200`), guarded navigation | `frontend/src/integrations/element-plus/` |
| **L1** | Implement `ApplicationDescriptor`, identity, routes, and read ports | Same-actor queries, `TurnTables` SQL, `<DataTable>` output | `testapps/contacts/adapter.py` |
| **L2** | Add `domain_playbooks/*.md` and `entity_rules` | Terminology disambiguation, precondition explanations, clickable `need_input` pills | `examples/yudao/` |
| **L3** | Add journal, postcondition verifier, and write contracts | Typed Skill writes, flat `update_record`, native submit, undoable prefill | `src/web_operation_agent/business/write_pipeline.py` |

## 2. L0: Panel Embedding and UI Helpers

Include the versioned same-origin Runtime script in the host page:

```html
<script
  type="module"
  src="/woa-agent/v1/runtime/assistant.js"
  data-woa-bootstrap="/woa-agent-boot/bootstrap"
></script>
```

On load, the Runtime requests `data-woa-bootstrap`. The host validates the signed-in session on the server and returns a short-lived bootstrap configuration from `EmbeddedGateway.bootstrap_config(...)`; no model keys appear in HTML or JS assets. The host also serves a pinned `PageController` ES module exporting `PageController` and `getElementByIndex`.

### Reusing or Adding UI Framework Helpers

The generic Runtime (`frontend/src/runtime/`) contains no component-library selectors. For Vue 3 + Element Plus applications, reuse the five modules in `frontend/src/integrations/element-plus/`:

- `table.js`: Reads table headers, visible rows, and row entity bindings.
- `select.js`: Samples visible options from `body`-mounted dropdown poppers (`readElSelectVisibleCandidates`) and avoids inner input focus loops (`shouldAvoidSelectInnerInput`).
- `date-picker.js`: Recognizes date and date-range inputs (`isElDateRangeInput`) and dispatches compliant change events.
- `form.js`: Reads form labels and drawer offset `--woa-drawer-offset` within bounded traversal limits (`depth: 8`, `nodes: 6000`, `bytes: 200000`).
- `vue.js`: Synchronizes framework-controlled component state.

For other UI libraries, add a sibling directory under `frontend/src/integrations/<app>/` exporting `readElementPageControl`, optional `openPage(url)`, and `readPageBlocker()`.

## 3. L1: Minimal Same-Actor Read Adapter

`testapps/contacts/adapter.py` provides a lightweight REST/HTML adapter structurally distinct from `examples/yudao`:

1. **Declare `ApplicationDescriptor`:** Bind `application_id`, `public_origin`, `navigation_mode` (`"document"` or `"spa"`), `page_controller_module`, and `business_tools.catalog_sha256` to enforce version alignment.
2. **Wire `ApplicationPorts` via `bind_application(adapter)`:**
   - `IdentityPort`: Maps trusted same-origin session headers or cookies to `PrincipalContext(tenant_id, actor_id, scopes)` without trusting client-reported user IDs or DOM text.
   - `ApplicationRoutePort`: Validates allowed in-app routes and declares `session_handoff_query_keys` stripped before logging or observations.
   - `EntityReadTools` / `ReferenceLookupTools` / `PageEntryTools` / `GoalCandidateTools`: Registers read tools returning `QueryEvidence` (`matched_count`, `count_exact`, `looked_for`, `exact_matches`), pairing with `bind_matching_identity` for `TurnTables` SQL and `<DataTable>`.
   - `GoalPort.capabilities()`: Declares only genuinely supported optional capabilities (see [Tools, Capabilities & Checks](../reference/tools-and-capabilities.md)).

## 4. L2: Domain Playbooks and Disambiguation

L2 resolves ambiguous terminology and duplicate entity names:

- **Domain playbooks (`domain_playbooks/*.md`):** Concise per-module definitions (for example distinguishing CRM Customers, Leads, and ERP Counterparties) loaded on demand via `load_domain_guide`.
- **Compiled rules (`entity_rules`):** Server validation preconditions extracted under an allowlisted `SourceTree` so the assistant can explain why a delete or approval action is blocked.
- **Disambiguation pills:** When a name lookup matches 2–4 real same-actor records, the model calls `finish(status="need_input", options=[...])` to render clickable buttons above the input box before continuing.

## 5. L3: Verified Writes and Form Handover

Enable writes (`mode = "verified-operate"`) only after L1 read contracts and identity isolation pass. All write routes execute through `WritePipeline` (see [Write Safety & Privacy](../security/verified-writes.md)) via three integration surfaces:

1. **Typed write Skills (`SKILL.md` + `agents/woa.toml`):** For multi-field or rule-heavy business writes.
2. **Symmetric flat convention updates (`ConventionWritePort`):** Declaring `"updates"` in `GoalPort.capabilities()` derives `update_record` for 1:1 symmetric flat entities (excluding nested lines and secret fields) and `toggle_record_status` for two-state reversible status fields.
3. **Native form submit and undoable prefill (`SubmitPort`):** Uses `SubmitConventions` to guard existing page forms—clicking submit once after approval and readback when verifiable, or falling back to undoable prefill handover (`undoFormFill`) when the user requests it or when password/upload controls are present.

Implementation details for all three write paths are in [Skills & Typed Writes](./skills-and-writes.md).

## Pre-Merge Adapter Checklist

Before merging a new adapter, run non-paid checks against synthetic data:

- Core neutrality check: `scripts/check_core_boundary.py`
- Adapter contracts and unit tests: `scripts/check.py`
- Browser end-to-end checks: L0 panel mount, L1 table filtering, L2 choice pills, L3 approval dispatch and readback
