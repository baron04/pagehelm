---
title: Security & Verified Writes
description: Safety modes, the 8-stage WritePipeline, visibility-gated approval cards, credential redaction, and prompt-injection defenses.
---

> **TL;DR:** Browser and backend mutations default **off**. Every state-changing write executes through a unified, single-flight **8-stage `WritePipeline`** requiring structured diff projection, fresh human confirmation on a visibility-gated panel card (`coverage >= 0.9`, `dwell >= 400ms`, `event.isTrusted === true`), pre-dispatch reread drift checks, zero automatic retry on uncertain outcomes, and same-actor readback verification.

[简体中文](../zh-cn/security/verified-writes.md)

## 1. Execution modes

The server enforces one of three write modes at session/composition binding time (`assistant.js` cannot self-escalate):

| Mode | Same-actor API & SQL reads | Navigation & filters | Non-destructive form prefill (`undoFormFill`) | Confirmed backend / submit mutations | Postcondition readback |
| --- | --- | --- | --- | --- | --- |
| `read-only` | Yes | Yes | Blocked | Blocked | N/A |
| `assisted-draft` | Yes | Yes | Yes (skips secrets, reversible) | Blocked | N/A |
| `verified-operate` | Yes | Yes | Yes | **Only via 8-stage `WritePipeline`** | Required (`ResultPostconditionVerifier`) |

Prompts, Skill markdown files, repository source snippets, and retained `ConversationState` turns carry **zero write authority**. Enabling mutations requires explicit deployment and adapter opt-in.

---

## 2. The 8-stage `WritePipeline`

`WritePipeline` (`src/web_operation_agent/business/write_pipeline.py`) governs both typed Skill/adapter service writes (`run_service_write`), convention flat updates & status transitions, and native UI form/row submits (`run_submit_pipeline`):

```text
  [1] Proposal Preparation & Structured Diff Normalization
        - FieldDiff / change_detail / create_detail / preserved_detail /
          side_effect_detail / derived_detail / target_rule_detail / body_detail
        - Bind exact proposal.content_digest & canonical target identity
         |
         v
  [2] Secret & Target Policy Filtering
        - Reject or exclude DEFAULT_SECRET_FIELD_PATTERN controls
        - Validate value-source provenance & append-preservation rules
         |
         v
  [3] Visible Approval Card Request (ApprovalCard.tsx)
        - Render structured old -> new diffs + side effects inside Shadow DOM
         |
         v
  [4] Single-Use Approval Grant & Single-Flight Gate
        - VisibilityGate requires minVisibleRatio >= 0.9, dwell >= 400ms, event.isTrusted
        - InflightWriteTracker.single_flight(proposal.content_digest, consume_on_enter=True)
        - ApprovalGate / approval_id consumed once (409 on replay)
         |
         v
  [5] Pre-Dispatch Reread & Drift Detection
        - Live read compares mutable fields against approved snapshot
        - Any unexpected change raises drift refusal BEFORE dispatch
         |
         v
  [6] Single-Flight Armed Dispatch (Zero Automatic Retry)
        - Write `mutation_claimed` into SQLiteMutationJournal FIRST
        - Timeout/disconnect NEVER triggers an automatic retry
         |
         v
  [7] Same-Actor Readback Postcondition Verification
        - ResultPostconditionVerifier / BusinessPostconditionVerifier
        - Confirm actual backend state reflects expected values under the same actor
         |
         v
  [8] Audited Terminal Accounting
        +--> verified      (postcondition confirmed; receipt recorded)
        +--> indeterminate (outcome uncertain; preserved for operator reconciliation)
        +--> refused / cancelled (no dispatch or drifted prior to dispatch)
```

---

## 3. Approval card visibility gate & UI hardening

Simply requiring a confirmation click is vulnerable if a hostile script scrolls the card offscreen, overlays another element, or synthesizes a synthetic `.click()`. `frontend/src/state/visibility.ts` enforces client-side visual integrity using parameters exported from `frontend/src/presentation.json`:

- `min_visible_ratio: 0.9` — computed across ancestor overflow boxes (`auto|scroll|hidden|clip`) and `IntersectionObserver` thresholds (`0..1` in 20 steps), clamping by whichever is smallest of card box, viewport, and clipping scrollport.
- `min_visible_ms: 400` — card must remain visibly exposed for at least `400 ms` continuously before an approval click is accepted.
- `event.isTrusted === true` — synthetic DOM click events dispatched via scripts are refused immediately.
- **Shadow DOM isolation:** Panel styles and interactive approval controls render inside a Shadow DOM container (`overlay_z_index: 2147483646`) separated from host page stylesheets.

---

## 4. Credential, handoff & egress redaction

PageHelm defends against credential leakage in URLs, DOM snapshots, logs, trace replays, and outbound LLM prompts:

1. **Handoff & secret assignment redaction (`src/web_operation_agent/privacy.py`):**
   - `SESSION_HANDOFF_KEYS` (`externalLoginKey`, `externaLoginKey`, `externLoginKey`, `jsessionid`) forms the closed baseline, widened per adapter via `SessionHandoffPolicy.extending(port.session_handoff_query_keys)`.
   - `SENSITIVE_CREDENTIAL_ASSIGNMENT` matches secret assignments (`password`, `passwd`, `pwd`, `secret`, `token`, `access_token`, `refresh_token`, `id_token`, `api_key`, `authorization`) across literal and multi-encoded URL parameters (`has_unredacted(..., url_decode_depth=3)` decodes nested percent-encoding before checking).
2. **Model egress auditing (`src/web_operation_agent/modeling/egress.py`):**
   - `AuditedChat` wraps the model client (forwarding both `complete` and `complete_cancellable`) and runs `ProviderEgressPolicy.inspect(messages, tools)` before sending to any external LLM endpoint, appending only non-content metadata (`provider_egress`) to the session ledger.
3. **Trace & log hygiene (`src/web_operation_agent/state/traceview.py`):**
   - Before writing replay files or timelines, trace exporters re-scan sanitized output with `url_decode_depth=3` and refuse to persist raw payload text if an unredacted credential pattern remains.

---

## 5. Opt-in `SourceTree` allowlist

Optional source inspection for troubleshooting or schema confirmation never receives unrestricted filesystem access:

- `SourceTree` (`src/web_operation_agent/planning/sourcetree.py`) requires an explicit absolute `source_root` injected by the host at runtime (policies themselves are forbidden from hardcoding `"root"`).
- Adapters ship packaged policies under `policies/<name>.json` declaring explicit `allow_globs`, `deny_globs`, and `max_bytes_per_read` (`262144` bytes default).
- `SourceTree.resolve(rel)` verifies **both** the requested path string `rel` and its canonical resolved path against `deny_globs` and `allow_globs`, blocking symlink/relative-path traversal (`views/Foo.vue -> ../secrets/Foo.vue`) from laundering denied secrets, `.env` files, databases, uploads, or user data.

---

## 6. Prompt-injection privilege separation

Browser page titles, customer notes, Skill markdown instructions, and model tool outputs are all treated as **untrusted inputs**:

- Untrusted text cannot mint an `approval_id`, alter `ExecutionMode`, add `allow_globs` to `SourceTree`, or satisfy an approval gate.
- Structured field diffs displayed on the approval card are normalized and validated on the server (`validate_details`), not trusted verbatim from unverified browser strings.
- Unknown or out-of-policy keys in protocol payloads trigger typed `RemoteProtocolError` responses rather than best-effort parsing.

---

## 7. Production CSP checklist

When embedding `assistant.js` into a strict Content Security Policy deployment:

- Serve `assistant.js` and `assistant.css` from the same origin or explicitly trusted gateway origin (`script-src 'self'`, `style-src 'self'` — runtime code uses Shadow DOM styling without requiring `unsafe-eval`).
- Allow `connect-src 'self'` (or your reverse-proxied gateway prefix) for `/v1/sessions/*` POST calls and SSE `/events` streams.
- Pair same-site authenticated cookies with reverse-proxy TLS termination (`HTTPS`) and configure `start.final_statuses: [401, 403]` behavior cleanly when user sessions expire.

---

## Next steps

- Review SQLite backups, indeterminate-write resolution, spend limits, and OTLP telemetry in [Operations & Observability](./operations-and-observability.md).
- Inspect exact `/v1/sessions/*` wire payloads and limits in [Protocol & Configuration Reference](../reference/protocol-and-configuration.md).
