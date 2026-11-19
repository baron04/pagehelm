---
title: Architecture Overview
description: Trust boundaries between the browser Runtime, application-neutral server core, and business adapters, plus turn flow and scope limits.
---

PageHelm is divided into three layers: the **first-party browser Runtime** (`assistant.js`), an **application-neutral server core** (`src/web_operation_agent/`), and an **application adapter**. Explicit wire protocols and port contracts separate these layers so the browser holds no model keys or multi-step plans and the server core stays independent of any specific application.

## 1. Three-Layer Architecture

```text
+-----------------------------------------------------------------------+
| First-Party Browser Tab (Same Origin)
|  - Embedded Runtime & Shadow DOM panel (assistant.js)
|  - PageController bounded snapshots (<= 200 elements) & fixed UI tools
|  - Web Locks + sessionStorage duplicate-tab session isolation
|  - ApprovalCard visibility gate (coverage >= 0.9, dwell >= 400ms, isTrusted)
+-----------------------------------------------------------------------+
             ^ HTTPS /v1/sessions/<id>/* + SSE (/events)
             | (redacted observations, receipts, approvals; no keys or plans)
             v
+-----------------------------------------------------------------------+
| Application-Neutral Server Core (src/web_operation_agent/)
|  - Transport gateway & session lease state machine
|  - ModelDrivenTurnExecutor + TurnHarness + TurnBudget
|  - AuditedChat egress audit & SessionHandoffPolicy redaction
|  - WritePipeline (proposal checks, approval, single-flight, reread, readback)
|  - SQLiteSessionStore, SQLiteMutationJournal, SQLiteSpendCap
+-----------------------------------------------------------------------+
             ^ ApplicationPorts SPI (explicit capability declarations)
             v
+-----------------------------------------------------------------------+
| Application Adapter & Domain Skills
|  - ApplicationDescriptor, IdentityPort, ApplicationRoutePort
|  - EntityReadPort, ReferenceLookupTools, TurnTables SQL / summarize
|  - ResultPostconditionVerifier, ConventionWritePort / SubmitPort
|  - Optional allowlisted SourceTree, domain_playbooks, and SKILL.md
+-----------------------------------------------------------------------+
```

Data reads and UI interactions are split by role: large aggregations and multi-table joins run on the server under the signed-in user's identity via typed read ports, while navigation, filtering, and form prefill execute in the user's live tab.

## 2. Single-Turn Sequence

```text
Operator (Browser)           assistant.js Runtime            PageHelm Server            Model Provider            Host Backend API
       |                              |                              |                          |                        |
       |--- Enter task goal --------->|                              |                          |                        |
       |                              |-- POST /messages + lease --->|                          |                        |
       |                              |                              |-- AuditedChat egress --->|                        |
       |                              |                              |<-- tool call (read/UI) --|                        |
       |                              |                              |-- same-actor read query ------------------------->|
       |                              |                              |<-- rows / total / page ---------------------------|
       |                              |                              | (TurnTables SQL / Decimal)                        |
       |                              |<-- SSE panel_state / action -|                          |                        |
       |                              |-- ActionReceipt + obs ------>|                          |                        |
       |                              |                              |-- WritePipeline proposal |                        |
       |<-- Render ApprovalCard ------|<-- SSE approval_request -----|                          |                        |
       |--- Trusted human click ----->|                              |                          |                        |
       |                              |-- POST /approvals (1x) ----->|                          |                        |
       |                              |                              |-- pre_dispatch_reread --------------------------->|
       |                              |                              |-- single-flight dispatch ------------------------>|
       |                              |                              |-- same-actor readback --------------------------->|
       |<-- Verified result / table --|<-- SSE finish ---------------|                          |                        |
```

Every tool receipt and settled page observation feeds back into the same server turn loop before the next model decision. The browser never advances a multi-step plan on its own.

## 3. Server vs. Browser Responsibilities

| Responsibility | Server core | Browser Runtime | Notes |
| --- | --- | --- | --- |
| Prompts, model API keys, tool routing | Yes | No | Frontend bundles contain no model keys or planners |
| Session auth and safety mode policy | Yes | No | Server locks `read-only` / `assisted-draft` / `verified-operate` |
| Business entities and field semantics | Adapter + Skills | No | Generic frontend carries no app-specific models |
| DOM snapshots and element collection | No | Yes | Bounded by `max_elements = 200` |
| Panel states, copy, and progress keys | Yes (`panel_contract.json`) | Render only | Projected over v2 `/timeline` and `/events` |
| Approval visibility & trusted-click gate | Policy (`presentation.json`) | Enforced in client | Coverage `>= 0.9`, dwell `>= 400ms`, `event.isTrusted` |
| Credential & handoff query redaction | Before storage & egress | Before gateway upload | `SessionHandoffPolicy` + recursive URL-decode checks |
| Persistent journals and spend caps | Yes (SQLite) | No | `SQLiteMutationJournal` + `SQLiteSpendCap` |

## 4. Tab Isolation and Conversation Boundaries

### Duplicate-Tab Isolation (Web Locks)

Storing a session ID only in `sessionStorage` causes a browser's "Duplicate Tab" action to inherit the original tab's session and lease, making two tabs compete over one SSE stream. `frontend/src/runtime/tabid.ts` handles this with the Web Locks API:

1. The tab ID is saved in `sessionStorage` (`woa.assistant.tab.v1`) so normal page reloads keep the same session.
2. On startup, `tabId()` requests `navigator.locks.request('woa.assistant.tab.' + id, { ifAvailable: true }, ...)`:
   - **Normal reload:** The previous document releases the lock on `unload`, and the reloaded page reclaims the same ID and session.
   - **Duplicate tab:** The original tab still holds the lock; the duplicate fails to acquire it, clears copied local session state, generates a fresh ID with `crypto.randomUUID()`, and starts an isolated session.

### Multi-Turn Conversation Bounds

`ConversationState` (`src/web_operation_agent/state/conversation.py`) caps retained context:

- **Defaults:** `max_turns = 4`, serialized JSON limit `32 KiB`, business snippet limit `16 KiB` (configurable maxima: `20` turns, `256 KiB`, `128 KiB`, with business bytes `<= max_bytes`).
- **No authority carryover:** Retained turns resolve references like "it" or "the previous row" only; each new turn must re-read live server facts or fresh page observations under the active actor.
- **New conversation:** `POST /v1/sessions/<id>/conversation` with `{"action": "new"}` clears turns, `pending_clarification`, and `page_operation_granted` while preserving persisted `SessionLedger` audit and spend records.

## 5. Core Neutrality Static Checks

`scripts/check_core_boundary.py` enforces layer boundaries in dev and CI checks:

1. `src/web_operation_agent/*.py` (excluding comments and docstrings) cannot reference specific reference application names or API prefixes.
2. Core code and core unit tests cannot import `web_operation_agent_*` adapter packages or `evals`.
3. `frontend/src/runtime/` cannot hardcode application routes or Element Plus `.el-*` selectors; UI framework helpers live under `frontend/src/integrations/`.
4. Built documentation output cannot contain local filesystem paths or local file URIs.

## 6. Explicit Out-of-Scope Cases

| Case | Handling |
| --- | --- |
| Cross-origin external sites or CAPTCHAs | Refused or handed back to the operator; never bypassed |
| Truncated server pagination (`truncated=True`) | Cannot report a partial slice as a full total; must fetch more or disclose partial coverage |
| Multiple matching write targets or missing required fields | Emits `finish(status="need_input", options=[...])`; never guesses values |
| Timeout or disconnect after `mutation_claimed` | Recorded as `INDETERMINATE`; never auto-retried |
| Optional capability omitted from `capabilities()` | Explains the limitation; adapters never fake support |

## Next Steps

- Turn loop, temporary SQL, and browser settling: [Execution & Browser Runtime](./execution-and-runtime.md)
- Write safety pipeline and privacy controls: [Write Safety & Privacy](../security/verified-writes.md)
- Integrating a host application: [L0–L3 Integration Guide](../integration/levels.md)
