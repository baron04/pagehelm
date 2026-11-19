---
title: Architecture Overview
description: How PageHelm separates server planning, the browser Runtime, and application adapters to deliver hybrid same-actor reads and safe UI actions.
---

> **TL;DR:** PageHelm splits responsibilities cleanly across three layers: a **first-party browser Runtime** (`assistant.js`) for DOM observation and fixed UI primitives, an **application-neutral Server** for model planning, policy, approvals, and audit, and a thin **Application Adapter** providing identity, routes, reads, and verified writes.

[简体中文](../zh-cn/architecture/overview.md)

## Three-layer architecture

```text
+-------------------------------------------------------------------+
| First-party Page (Same Origin)                                    |
|  - Embedded Runtime + Shadow DOM Panel (assistant.js)             |
|  - PageController semantic snapshot & fixed UI tools              |
|  - Approval card visibility dwell + event.isTrusted gate          |
+-------------------------------------------------------------------+
          ^ authenticated observations / actions / decisions (HTTPS)
          v
+-------------------------------------------------------------------+
| Application-Neutral Server (web_operation_agent)                  |
|  -TurnExecutor & TurnHarness  | Outbound secret redaction         |
|  -WriteSafetyPipeline         | Budget, quota & SQLite stores     |
|  -ProgramCheckSpec checks     | TurnTrace & timeline projection   |
+-------------------------------------------------------------------+
          ^ typed ports (ApplicationPorts & GoalData)
          v
+-------------------------------------------------------------------+
| Application Adapter (e.g. examples/yudao, testapps/contacts)      |
|  - Identity & route validation | Same-actor API & SQL reads       |
|  - Domain playbooks & Skills   | Convention & form write handlers |
+-------------------------------------------------------------------+
```

### Design principles

1. **Credentials and knowledge never enter the browser:** Model API keys, prompts, Skill manuals, source indexes, and evaluation expectations remain on the server. A compromised DOM script cannot read model secrets or hidden application rules.
2. **Every tool result returns to the same server agent:** Every click, navigation, API read, or approval decision produces a typed receipt plus a fresh page observation fed back before the model chooses its next action.
3. **Runtime facts outrank static prose:** Observed URLs, live DOM state, and same-actor API readbacks supersede Skill documentation or compiled source hints whenever they disagree.
4. **Application neutrality in core:** Neither `src/web_operation_agent/` nor `frontend/src/runtime/` contains application names, business routes, or UI-framework CSS selectors (enforced automatically by `scripts/check_core_boundary.py`).

## Hybrid execution: same-actor APIs + semantic DOM

PageHelm uses a single ReAct turn executor (`ModelDrivenTurnExecutor` paired with `TurnHarness`) across two complementary data/action paths:

- **Same-actor API and SQL reads:** Read tools such as `call_api` execute on the server using the signed-in user's delegated token (never a shared superuser key), so row-level permissions and tenant boundaries come directly from the host backend. Tabular results load into per-turn in-memory SQLite (`TurnTables`) queryable via `tables` / `sql` / `date_range`, and large structured responses attach directly to `finish` as a `<DataTable>` payload (client-side 10/25/50/all pagination, filter, sort, TSV/CSV export) instead of asking the model to transcribe long lists. Independent pure reads within one model reply execute concurrently in worker threads and commit sequentially on the turn thread.
- **Semantic DOM navigation and form prefill:** Browser actions (`goto`, `click`, `input`, `select`, `fill_fields`, `scroll`, `highlight`) address interactive elements by epoch-scoped semantic references (`PageController`). Controls inside active dialogs and popovers are prioritized within the per-observation budget; settled actions include a fresh observation summary directly in the action receipt (`settled_action_observation`) to eliminate redundant `observe` round-trips. Wait instrumentation suppresses same-value attribute rewrites and long background connections so `settle` returns rapidly.

## Session isolation & observability

- **Per-tab isolation:** Each browser tab holds a capability-bound session tied to `(application_id, principal_digest, runtime_instance_id)` stored in `sessionStorage` with Web Lock deduplication across duplicate tabs. Opening a second tab never steals or cancels another tab's active turn.
- **Bounded history without stale facts:** `ConversationState` keeps at most the last 4 turns (`32 KiB` total, redacted before persistence) as conversational pointers only; numeric facts and UI routes from prior turns are never treated as live truth without re-reading.
- **Structured timeline & tracing:** `TurnTrace` records per-step model calls, tool durations, wait/UI timings (`ARC-08`), `first_route_kind`, `ProgramCheckSpec` checks, and redacted payloads for HTML replay via `trace_replay.html`.

## Current operational boundaries

- **Single-node deployment model:** Session stores, `SQLiteMutationJournal`, and provider quotas use local SQLite (`WAL`); multi-node active-active session migration is not supported.
- **First-party trust assumption:** The panel isolates styles with Shadow DOM and requires trusted user gestures (`event.isTrusted`) plus continuous visibility on approval cards, but shares the host document's JS realm. Host pages must remain free of XSS and configure strict CSP headers ([Security & Verified Writes](../security/verified-writes.md)).
- **Alpha accuracy scope:** Reference integrations include the full-featured `yudao` adapter and the synthetic `contacts` test app; passing integration contracts does not establish general benchmark accuracy across unseen tasks.
