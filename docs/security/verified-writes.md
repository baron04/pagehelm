---
title: Security & Verified Writes
description: Default-off writes, the 8-stage WritePipeline, mode tiers, outbound credential redaction, Shadow DOM trust limits, and indirect prompt-injection defense.
---

> **TL;DR:** Mutations default **off** (`read-only`). Enabling `verified-operate` still enforces an **8-stage `WritePipeline`**: every persistent write requires an exact effect projection, a fresh human approval card (`event.isTrusted` + visibility dwell), single-consume SQLite CAS dispatch, pre-dispatch re-read, same-actor readback, and strict **never-retry** semantics on `INDETERMINATE` outcomes.

[简体中文](../zh-cn/security/verified-writes.md)

## Safety modes

| Mode (`pagehelm.toml` / CLI) | Page observation & nav | Form prefill | Write proposals & approvals | Backend mutation dispatch |
| --- | --- | --- | --- | --- |
| `read-only` (production default) | Allowed under read-only window & GET fence | Blocked (`native_edit_prefill=false`) | Blocked (`native_writes=false`) | Blocked (`native_submit=false`) |
| `assisted-draft` | Allowed under read-only window & GET fence | Allowed for verified draft targets (`native_edit_prefill=true`) | Prepared only (`native_write_preparation=true`) | Blocked (never dispatches mutations) |
| `verified-operate` (local default) | Allowed under read-only window & GET fence | Allowed | Full typed Skill, convention, and native submit proposals | Single-dispatch after fresh approval (`native_writes=true`, `native_submit=true`) |

> **Never trust prompt text for authorization:** System prompts, Skill Markdown files, previous turns, API responses, and DOM contents grant **zero** permission to mutate data. Even in `verified-operate`, destructive actions (delete, void, role/permission changes) require explaining impact and obtaining explicit confirmation before creating a card.

## The 8-stage `WritePipeline`

Every write route (Skill operations, symmetric `ConventionWritePort` updates, and native `SubmitPort` submissions) passes through `BusinessWriteService`:

1. **Value provenance check:** Every drafted field value must trace to the user's literal request (including normalized date/amount intervals), a user-selected candidate, an explicit tool conversion receipt, or an append-only preservation check over the existing record. Missing or ambiguous values halt for `needs_input` clarification—never guessed.
2. **Read-only preparation & effect projection:** Under the user's own `PrincipalContext`, the adapter resolves exact record IDs, reads current field values, builds the minimal mutation payload, and projects a human-readable diff (`原值 → 新值` with unchanged fields collapsible).
3. **Locked proposal digest:** Target identity, actor principal, turn ID, request shape, and projected effect hash into an immutable proposal digest (`proposal_digest`). Any deviation invalidates the approval.
4. **Fresh visible approval card (`event.isTrusted` + dwell gate):** The panel renders one card showing exact targets and changes. Clicking **Approve** succeeds only when backed by a real browser user gesture (`event.isTrusted === true`) after the card has remained continuously and substantially visible (`useVisibilityGate`); obscured cards (`--woa-drawer-offset` / overlap checks) refuse clicks. Cards expire after a fixed TTL and cannot be saved across turns or sessions.
5. **Single-consume atomic claim (`SQLiteMutationJournal`):** Approval tokens are consumed **once**. SQLite transactions atomically transition `prepared -> dispatching` while blocking concurrent duplicate business keys (`dispatching` or `indeterminate`).
6. **Pre-dispatch re-read:** Immediately before sending the write, the server re-reads the target record (and live form state for `SubmitPort`) and aborts if concurrent modification occurred since card display.
7. **Single dispatch (max once):** The approved mutation executes at most once under a window-scoped guard permit (for native submits, strictly matching the approved endpoint, record ID, and diff).
8. **Same-actor readback & outcome handling:** The server reads the target record back as the same actor to prove every required postcondition. Incomplete readbacks, HTTP 5xx, network disconnects, or timeouts record **`INDETERMINATE`** cleanly—**never automatically retried**. Users can inspect `INDETERMINATE` status read-only via the panel's reconcile action or CLI `--writer-stopped` resolution.

## Outbound privacy & prompt-injection isolation

- **Zero credential egress (`AuditedChat` + `privacy.py`):** Before any payload reaches a model provider or disk log (`conversation-state.sqlite3`, `TurnTrace`, `audit.jsonl`), recursive URL-decoding redaction strips passwords, bearer tokens, API keys, and configured `session_handoff_query_keys` (`[REDACTED]`), rejecting dispatch if any plaintext assignment survives secondary inspection. `<input type="password">` and file upload fields are stripped during DOM capture and never autofilled.
- **Control plane vs. untrusted data plane:** All DOM snapshots, API responses, Skills, and source snippets are marked untrusted application data. The Runtime has no arbitrary JS `eval`, no external `fetch`, and enforces strict same-origin navigation (`origins.py`, blocking `javascript:` and foreign URLs), neutralizing data exfiltration from indirect prompt injections inside records.
- **Host CSP & Shadow DOM boundary (explicit model):** The panel renders inside a closed/isolated `ShadowRoot` and runs cleanly under strict host CSP (`script-src 'self'`, `style-src 'self'`, `require-trusted-types-for 'script'`). Because an embedded widget shares the host page's JS realm, first-party host pages must stay free of XSS—this embedded panel is explicitly **not** an untrusted third-party site isolator.
