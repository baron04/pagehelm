---
title: PageHelm Documentation
description: Embedded AI operations helm (web_operation_agent) with server-owned planning, same-actor APIs + semantic DOM execution, and fresh visible write approvals.
template: splash
hero:
  title: PageHelm
  tagline: Embed Inside First-Party Apps. Read at API Speed, Mutate Behind Proof.
---

[简体中文](zh-cn/index.md)

## Why Same-Origin Hybrid Beats Both Chatbots and Headless RPA

Enterprise back-office applications span hundreds of routes, row-level permission scopes, client-side currency/enum formatters, and multi-line dialogs. Pure API chatbots and server-side headless browsers fail on opposite halves of the problem:

| Capability | API-Only Chatbot | Headless Server RPA | Client-Only DOM Agent | **PageHelm (Same-Origin Hybrid)** |
| --- | --- | --- | --- | --- |
| **Credential custody** | Often service account | Server stores user cookies (high risk) | Model keys exposed or proxied cleanly? Varies | **Server keeps model keys; browser keeps login cookies** |
| **Multi-thousand-row aggregation** | Fast API queries, no UI | Slow page-by-page DOM clicks | Browser DOM limits & truncation | **Same-actor concurrent `call_api` + in-memory SQL (`TurnTables`)** |
| **Live dialogs & form prefill** | Unsupported | Detached browser, cannot hand back live page | Can touch host page directly | **Epoch-scoped `PageController` + Element Plus integration + `undoFormFill`** |
| **Persistent mutations** | Needs ad-hoc endpoints | Unconstrained clicks on Save/Delete | Hard to enforce server CAS | **8-stage `WritePipeline`, `event.isTrusted`, max-1 dispatch, same-actor readback** |
| **Timeout / uncertain writes** | Often blind retries | Ambiguous UI state on timeout | Partial click risk | **`SQLiteMutationJournal` records `INDETERMINATE`; never auto-retried** |
| **Outbound privacy** | Ad-hoc logging | Screenshots leak secrets | DOM tokens may leak to LLM | **Recursive URL-decoding redaction (`AuditedChat`) & password/file stripping** |

---

## Documentation Map

### Architecture & Concepts
- **[Architecture Overview](architecture/overview.md)** — Trust boundary between the application-neutral Python 3.13 server and the fixed browser Runtime, end-to-end turn lifecycle, per-tab isolation, and operational boundaries.
- **[Execution Engine & Browser Runtime](architecture/execution-and-runtime.md)** — Inside `ModelDrivenTurnExecutor`, `TurnHarness`, concurrent reads + sequential commits, `TurnTables` (`sql` / `<DataTable>`), Decimal `read_summary.py`, `ProgramCheckSpec` loop guards, and Element Plus `PageController` instrumentation.

### Quickstart & Integration
- **[5-Min Quickstart & Operations](integration/quickstart.md)** — Minimal 1–3 line `.env` + `pagehelm.toml` setup, dual isolated local sandboxes (`./local --synthetic-clock`), `./deploy` rollout, and `scripts/check.py` qualification lanes.
- **[L0–L3 Progressive Integration](integration/levels.md)** — Step-by-step adapter authoring guide from a single `<script>` tag (L0) to `ApplicationPorts` & `QueryEvidence` (L1), `domain_playbooks/` & choice pills (L2), and verified writes (L3).
- **[Authoring Skills, Conventions & Native Submissions](integration/skills-and-writes.md)** — Declarative `SKILL.md` + `agents/woa.toml`, `prepare_mutation` / `execute_mutation`, symmetric `ConventionWritePort` (`update_record` / `toggle_record_status`), and `SubmitPort` / `MutationPrefillOffer`.

### Security & Operations
- **[Security & Verified Writes](security/verified-writes.md)** — Three safety tiers (`read-only`, `assisted-draft`, `verified-operate`), the 8-stage `WritePipeline`, visibility dwell gates (`>=400ms`, `event.isTrusted`), recursive credential redaction, and Shadow DOM CSP requirements.
- **[Durable Operations & Observability](security/operations-and-observability.md)** — SQLite WAL online backups & isolated restores, resolving `INDETERMINATE` mutations (`--writer-stopped` / `/reconciliations`), `SIGTERM` drain, 3-tier `SQLiteSpendCap` (`turn_reserve_cny=0.02`), content-free OTLP telemetry, and `TurnTrace` HTML replays.

### Reference
- **[Protocol & Configuration Reference](reference/protocol-and-configuration.md)** — Strict `pagehelm.toml` schema, `.env` resolution rules, host CLI flags, `/v1/sessions/` REST & SSE wire examples, `panel_contract.json` v2 states/payloads, and hard bounds.
- **[Tools, Capabilities & Program Checks](reference/tools-and-capabilities.md)** — Complete catalog of built-in read, SQL, browser, Skill, submit, source, and `finish` tool contracts, `GoalPort.capabilities()`, and `ProgramCheckSpec` checks.

---

## Repository Layout

| Path | Responsibility |
| --- | --- |
| `src/web_operation_agent/` | Application-neutral core: turn loop, `TurnHarness`, `WritePipeline`, gateways, schemas, SQLite stores |
| `frontend/` | Browser Runtime (`assistant.js`), Shadow DOM React panel, `PageController`, and shared `element-plus/` helpers |
| `examples/yudao/` | Full-stack reference adapter with dual local Docker Compose sandboxes (`./local`) and release CLI (`./deploy`) |
| `testapps/contacts/` | Structurally distinct synthetic Contacts application verifying core contracts & Chromium integration |
| `docs/` & `frontend/docs/` | Single bilingual Markdown corpus and Astro Starlight site (with build-time `/llms.txt` & `/llms-full.txt`) |

> **Alpha scope:** PageHelm is pre-production software. Evaluate only against isolated, resettable synthetic fixtures—never connect customer accounts, live payment flows, or production databases.
