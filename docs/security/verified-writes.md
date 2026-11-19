---
title: Write Safety & Privacy
description: Safety modes, the WritePipeline stages, client visibility gates, credential redaction, SourceTree allowlists, and CSP requirements.
---

PageHelm disables all browser and backend mutations by default. When writes are enabled, every state change runs through `WritePipeline` (`src/web_operation_agent/business/write_pipeline.py`): build and validate a structured diff, obtain human confirmation on a visible approval card, verify no drift under a single-flight lock, dispatch at most once, and read back under the same actor.

## 1. Safety Modes

The server locks the active mode during composition and session binding; the browser cannot elevate permissions:

| Mode | Same-actor API / SQL reads | Navigation & filtering | Non-destructive form prefill (undoable) | Write proposal prep | Write dispatch & readback |
| --- | --- | --- | --- | --- | --- |
| `read-only` (prod default) | Allowed | Allowed | Off | Off | Off |
| `assisted-draft` | Allowed | Allowed | Allowed (skips passwords & uploads) | Allowed | Off (never dispatches) |
| `verified-operate` (local default) | Allowed | Allowed | Allowed | Allowed | Allowed (via `WritePipeline` with readback) |

Prompts, Skill files, source excerpts, and prior session turns grant no write permission. Both deployment configuration and the application adapter must opt in explicitly.

## 2. `WritePipeline` Stages

`WritePipeline` unifies all four write routes (typed Skill writes, convention flat updates/toggles, typed status operations, and native form submission) across six stages `(a)–(f)`:

```text
  (a) Proposal preparation & validation
        - Adapter builds BusinessMutationProposal bound to target IDs and content_digest
        - Normalizes diffs (normalize_approval_details) and checks effect projection (verify_effect_projection)
        - Checks value provenance and append-preservation; filters DEFAULT_SECRET_FIELD_PATTERN controls
        - Optionally prepares MutationPrefillOffer ("prefill only, I will submit")
         |
         v
  (b) Visible approval & single-use consumption
        - Shadow DOM panel renders Before -> After diffs and side effects
        - VisibilityGate enforces visible ratio >= 0.9, dwell >= 400ms, event.isTrusted === true
        - Approval consumes once (replay returns 409); user may also choose prefill handover or deny
         |
         v
  (c) Single-flight lock & journal claim
        - InflightWriteTracker.single_flight(proposal.content_digest, consume_on_enter=True)
        - Records mutation_claimed in SQLiteMutationJournal before dispatch
         |
         v
  (d) Pre-dispatch reread & at-most-once dispatch
        - pre_dispatch_reread checks target record/form for concurrent drift and refuses if changed
        - Dispatches at most once; timeouts or disconnects are never retried automatically
         |
         v
  (e) Same-actor readback verification
        - ResultPostconditionVerifier / verify_readback confirms the expected post-state as the same actor
         |
         v
  (f) Audit terminal state
        +--> verified      (confirmed via same-actor readback)
        +--> not_approved / refused / cancelled (stopped before dispatch)
        +--> indeterminate (outcome unknown after dispatch; persisted for human reconciliation)
```

## 3. Client Visibility and Trusted-Click Gate

Plain confirmation dialogs can be obscured by overlays, scrolled out of view, or triggered via scripted `.click()`. `frontend/src/state/visibility.ts` enforces the parameters defined in `frontend/src/presentation.json`:

- `min_visible_ratio: 0.9`: Walks clipping ancestors (`overflow: auto|scroll|hidden|clip`) alongside 20 `IntersectionObserver` thresholds; uses the minimum intersection of card, viewport, and scroll-port dimensions so both tall cards and partial occlusions are evaluated accurately.
- `min_visible_ms: 400`: The approval card must stay visibly unobstructed for at least `400 ms` before confirmation is accepted.
- `event.isTrusted === true`: Rejects synthetic script-dispatched clicks.
- **Shadow DOM isolation:** The panel and approval card mount in an isolated Shadow DOM container (`overlay_z_index: 2147483646`) separated from host stylesheets.

## 4. Credential Redaction and Egress Audit

Redaction runs across URLs, DOM observations, audit journals, trace exports, and outbound model calls:

1. **Handoff params & credential assignments (`src/web_operation_agent/privacy.py`):**
   - `SESSION_HANDOFF_KEYS` (`externalLoginKey`, `externaLoginKey`, `externLoginKey`, `jsessionid`) can be extended per adapter via `SessionHandoffPolicy.extending(port.session_handoff_query_keys)`.
   - `SENSITIVE_CREDENTIAL_ASSIGNMENT` matches `password`, `passwd`, `pwd`, `secret`, `token`, `access_token`, `refresh_token`, `id_token`, `api_key`, and `authorization` assignments, using recursive URL decoding (`url_decode_depth=3`) to catch multi-encoded values.
2. **Content-free model egress audit (`src/web_operation_agent/modeling/egress.py`):**
   - `AuditedChat` wraps the model client (`complete` and `complete_cancellable`), running `ProviderEgressPolicy.inspect(messages, tools)` before dispatch and appending content-free `provider_egress` entries to the audit ledger.
3. **Trace export pre-write check (`src/web_operation_agent/state/traceview.py`):**
   - Rescans projected fields at `url_decode_depth=3` before writing replay HTML; refuses raw output if any credential pattern remains.

## 5. Optional `SourceTree` Allowlist

When source-assisted inspection is enabled, `SourceTree` (`src/web_operation_agent/planning/sourcetree.py`) restricts reads to an explicit allowlist:

- Policy files (`policies/<name>.json`) carry no local paths; the host injects `source_root` at startup.
- Policies configure `allow_globs`, `deny_globs`, and `max_bytes_per_read` (default `262144` bytes), excluding `.env`, databases, uploads, and user data.
- `SourceTree.resolve(rel)` evaluates `deny_globs` and `allow_globs` against both the raw relative path `rel` and the canonical resolved path to block symlink escapes.

## 6. Prompt-Injection and Protocol Isolation

Page DOM text, record notes, Skill Markdown, and free-form model output are all treated as untrusted data:

- Untrusted content cannot mint an `approval_id`, alter safety mode, widen `SourceTree` globs, or bypass the single-flight lock.
- Approval card diffs are validated on the server via `validate_details` rather than accepting arbitrary client strings.
- Unknown fields or out-of-bounds payloads raise typed `RemoteProtocolError` errors without falling back to exception-message matching.

## 7. Host CSP Checklist

When embedding into an application with a strict Content Security Policy:

- Allow loading `assistant.js` and `assistant.css` from the same origin or trusted gateway (`script-src 'self'`, `style-src 'self'`; `unsafe-eval` is not needed).
- Allow same-origin `/v1/sessions/*` requests and `/events` SSE streams in `connect-src`.
- Deploy behind HTTPS with same-origin cookies and handle session expiry according to `start.final_statuses: [401, 403]` in `frontend/src/presentation.json`.

## Next Steps

- Deployment, SQLite backups, and `INDETERMINATE` reconciliation: [Deployment & Operations](./operations-and-observability.md)
- Wire protocol endpoints and config schema: [Configuration & Protocol](../reference/protocol-and-configuration.md)
