---
title: Architecture Overview
description: How PageHelm separates server planning, the browser Runtime, and application adapters to deliver hybrid same-actor reads and safe UI actions.
---

> **TL;DR:** PageHelm splits responsibilities cleanly across three layers: a **first-party browser Runtime** (`assistant.js`) running inside the active tab for DOM observation and fixed UI primitives, an **application-neutral Server** owning model planning, policy, approvals, and audit, and a thin **Application Adapter** providing identity, routes, typed reads, and verified writes.

[简体中文](../zh-cn/architecture/overview.md)

## 1. Three-layer architecture

```text
+-----------------------------------------------------------------------+
| First-Party Browser Tab (Same Origin)                                 |
|  - Embedded Runtime + Shadow DOM Panel (assistant.js)                 |
|  - PageController bounded snapshot (<= 200 elements) & fixed UI tools |
|  - Tab lock (Web Locks + sessionStorage) & duplicate-tab isolation    |
|  - ApprovalCard visibility gate (ratio >= 0.9, dwell >= 400ms,        |
|    event.isTrusted === true)                                          |
+-----------------------------------------------------------------------+
             ^ HTTPS /v1/sessions/<id>/* + SSE (/events)
             | (observations, receipts, approvals; zero secrets/plans)
             v
+-----------------------------------------------------------------------+
| Application-Neutral Server Core (src/web_operation_agent/)            |
|  - Transport Gateway & Channel Lease State Machine                    |
|  - ModelDrivenTurnExecutor + TurnHarness + TurnBudget                 |
|  - AuditedChat egress policy + SessionHandoffPolicy redaction         |
|  - WritePipeline (8-stage approval, drift check, single-flight, read) |
|  - SQLiteSessionStore, SQLiteMutationJournal, SQLiteSpendCap          |
+-----------------------------------------------------------------------+
             ^ ApplicationPorts SPI (explicit capability declaration)
             v
+-----------------------------------------------------------------------+
| Application Adapter + Domain Skills (adapters / Skill packs)          |
|  - ApplicationDescriptor, IdentityPort, ApplicationRoutePort          |
|  - EntityReadPort, ReferenceLookupTools, TurnTables SQL / summarize   |
|  - ResultPostconditionVerifier, ConventionWritePort / SubmitPort      |
|  - Optional SourceTree allowlist & domain_playbooks / SKILL.md        |
+-----------------------------------------------------------------------+
```

Most enterprise assistant architectures fail at opposite extremes:

- **API-only bots** answer counts or lookups well, yet cannot open the right edit screen, drive client-side validation, or hand over an interactive prefilled form to the operator.
- **Headless RPA browsers** scrape paginated DOM tables slowly, hold privileged cookies outside the user's browser session, and burn context tokens walking through list screens one page at a time.

PageHelm uses **hybrid same-actor execution**: read-heavy questions run through server-side typed reads (`read_summary`, `TurnTables` SQL, `<DataTable>` exports) bound strictly to the browser user's active session identity, while screen navigation, filters, and form handoff execute live in the user's open tab under server planning.

---

## 2. End-to-end turn sequence

```text
Operator (Browser Tab)          assistant.js Runtime             PageHelm Server               Model Provider          App Backend API
        |                               |                              |                             |                        |
        |--- Enter prompt ------------->|                              |                             |                        |
        |                               |-- POST /messages + lease --->|                             |                        |
        |                               |                              |-- AuditedChat check ------->|                        |
        |                               |                              |<-- tool call (read/api) ----|                        |
        |                               |                              |-- same-actor read --------->|                        |
        |                               |                              |<-- rows / total / page -----|                        |
        |                               |                              | (TurnTables SQL / Decimal)  |                        |
        |                               |<-- SSE panel_state / action -|                             |                        |
        |                               |    (e.g., observe / prefill) |                             |                        |
        |                               |-- ActionReceipt + obs ------>|                             |                        |
        |                               |                              |-- WritePipeline propose --->|                        |
        |<-- Render ApprovalCard -------|<-- SSE approval_request -----|                             |                        |
        |    (ratio>=0.9, dwell>=400ms) |                              |                             |                        |
        |--- Trusted Click Approve ---->|                              |                             |                        |
        |                               |-- POST /approvals (1-use) -->|                             |                        |
        |                               |                              |-- pre_dispatch_reread ----->|                        |
        |                               |                              |-- single-flight dispatch -->|                        |
        |                               |                              |-- same-actor readback ----->|                        |
        |<-- Render Verified Diff / <DataTable> <-- SSE finish --------|                             |                        |
```

Every tool result and settled browser observation returns to the **same server turn loop** before the model makes its next decision. The browser never decides the next plan step on its own.

---

## 3. Trust and authority matrix

| Concern | Owned by Server Core | Owned by Browser Runtime | Notes |
| --- | --- | --- | --- |
| Model prompts, API keys & tool routing | Yes | No | Models never run inside the browser bundle |
| Session authorization & write mode | Yes | No | Server enforces `read-only` / `assisted-draft` / `verified-operate` |
| Business entity/field metadata | Adapter + Skills | No | Browser never embeds app schemas |
| DOM snapshot & interactive element map | No | Yes | Bounded to `MAX_OBSERVATION_ELEMENTS = 200` |
| Panel copy, tones & progress labels | Yes (`panel_contract.json`) | Renders only | Protocol v2 projected state via `/timeline` & `/events` |
| Approval card click-jacking guard | Policy (`presentation.json`) | Enforces visually | `min_visible_ratio: 0.9`, `min_visible_ms: 400`, `event.isTrusted` |
| Credentials & URL handoff keys | Redacts before model/logs | Redacts on egress | `SessionHandoffPolicy` + recursive decode checks |
| Persistent ledger & spend meter | Yes (`SQLite`) | No | `SQLiteMutationJournal` + `SQLiteSpendCap` |

---

## 4. Tab identity and duplicate-tab isolation

Enterprise operators frequently press **Duplicate Tab** in their browser. A naive `sessionStorage` token alone would clone the original tab's session identifier, causing two open tabs to collide on SSE cursors, leases, and approval prompts.

PageHelm prevents split-brain ownership in `frontend/src/runtime/tabid.ts`:

1. Each tab persists its current ID under `sessionStorage` key `woa.assistant.tab.v1` so same-tab reloads cleanly reclaim their ongoing session.
2. On boot, `tabId()` calls `navigator.locks.request('woa.assistant.tab.' + id, { ifAvailable: true }, ...)`.
3. A **same-tab reload** finds the previous document's lock released on unload and reclaims the same session.
4. A **duplicated tab** finds the lock still held by the original tab, triggers rotation (clearing cloned client session caches), mints a fresh `crypto.randomUUID()` tab ID, and binds a new isolated lease.

---

## 5. Multi-turn conversation state boundaries

`ConversationState` (`src/web_operation_agent/state/conversation.py`) intentionally bounds conversational memory to prevent subtle staleness and authority leakage across turns:

- **Defaults:** `max_turns = 4`, `max_bytes = 32 * 1024` (`32 KiB` total serialized JSON), `max_business_bytes = 16 * 1024` (`16 KiB` business slice).
- **Hard configurable ceilings:** `max_turns <= 20`, `max_bytes <= 256 KiB`, `max_business_bytes <= 128 KiB` (`max_business_bytes <= max_bytes`).
- **Zero inherited authority:** Retained history gives conversational continuity for pronouns or follow-up questions; every new turn must still re-observe the live page or query the application backend under the current actor before acting.
- **Clean reset (`POST /v1/sessions/<id>/conversation` with `{"action": "new"}`):** Clears retained turns, `pending_clarification`, and `page_operation_granted`, while preserving the immutable `SessionLedger` audit trail and spend records.

---

## 6. Core neutrality and boundary enforcement

PageHelm runs CI structural enforcement (`scripts/check_core_boundary.py`) on every change touching core Python or browser runtime modules:

1. **No application leakage in core Python:** An AST- and token-aware scanner inspects executable Python statements in `src/web_operation_agent/` (excluding comments/docstrings) and fails if application-specific product names or route paths appear.
2. **No adapter imports in core:** Core code and core tests must never import any `web_operation_agent_*` adapter distribution or `evals`.
3. **No UI framework coupling in browser runtime:** `frontend/src/runtime/` cannot hardcode reference-app URLs or Element Plus `.el-*` selectors; reusable UI library glue lives under `frontend/src/integrations/`.
4. **No local paths or unredacted fixtures:** Site checks verify no developer filesystem paths leak into build outputs or documentation bundles.

---

## 7. Explicit operational boundaries

| Situation | PageHelm behavior |
| --- | --- |
| Task requires cross-origin external websites or CAPTCHA solving | Refuses or asks operator to take over; never solves or bypasses anti-bot gates |
| Requested aggregate spans truncated/paginated slice (`truncated=True`) | Refuses to fabricate an exact total from a partial slice; widens query or states coverage explicitly |
| Ambiguous record/action target during a write or draft | Calls `finish(status="need_input", options=[...])` with verified candidates rather than guessing |
| Mutation times out after single-flight `mutation_claimed` | Marks transaction `INDETERMINATE`; never automatically retries |
| Undeclared adapter capability | Explains the boundary clearly without asking for unsupported tools |

---

## Next steps

- Dive into turn orchestration, concurrent reads, `TurnTables` SQL, and DOM settling in [Execution & Runtime](./execution-and-runtime.md).
- Inspect the 8-stage mutation fence in [Security & Verified Writes](../security/verified-writes.md).
- Connect your own backend step by step in [L0–L3 Progressive Integration](../integration/levels.md).
